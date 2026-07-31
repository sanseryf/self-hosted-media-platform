"""L3 — Jellyseerr policy auto-approver (SHADOW MODE by default).

    POST /api/policy/evaluate-pending   decide any un-decided pending requests  (session-gated)
    GET  /api/policy/decisions          the decision audit log                  (session-gated)
    GET  /api/policy/config             active rules + enforce flag             (session-gated)

Two safety flags, both OFF by default (deploy-time opt-in, like HUB_JELLYFIN_LOGIN_ENABLED):
  • POLICY_ENFORCE=false — when off, decisions are computed and LOGGED but never acted
    on. Shadow mode: you can watch what it *would* do before letting it touch anything.
  • No background loop — evaluation only runs when the endpoint is called, so it makes
    no outbound Jellyseerr calls on its own until you wire it up.

The decision logic is the pure, tested policy_rules.evaluate(); this module gathers
context best-effort (requester's pending count, disk headroom) via the L2 gateway —
any piece it can't get is simply skipped, never fatal — and records every decision
with its reasons, so the log is a full audit trail whether or not enforcement is on.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import connectors
import db
import policy_rules

router = APIRouter()
log = logging.getLogger("hub.policy")

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "policy_schema.sql"

POLICY_ENFORCE = os.environ.get("POLICY_ENFORCE", "false").strip().lower() in ("1", "true", "yes")


def _rules() -> policy_rules.Rules:
    """Build the rule config from env, falling back to conservative defaults."""
    def fnum(name, default):
        try:
            return float(os.environ.get(name, "")) if os.environ.get(name) else default
        except ValueError:
            return default
    deny = tuple(s.strip() for s in os.environ.get("POLICY_DENY_TYPES", "").split(",") if s.strip())
    return policy_rules.Rules(
        max_pending_per_user=int(fnum("POLICY_MAX_PENDING", 10)),
        min_disk_free_gb=fnum("POLICY_MIN_DISK_GB", 50.0),
        deny_media_types=deny,
        allow_4k=os.environ.get("POLICY_ALLOW_4K", "false").strip().lower() in ("1", "true", "yes"),
    )


async def ensure_schema() -> None:
    p = db.pool()
    if p is None:
        return
    try:
        async with p.acquire() as conn:
            await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass


async def _gather_ctx(ev, payload: dict) -> dict:
    """Best-effort decision context. Every lookup is wrapped — a missing piece
    just skips that gate (policy_rules handles None), never fails the decision."""
    ctx = {"media_type": ev["media_type"]}
    req = (payload.get("request") or {})
    media = (payload.get("media") or {})
    ctx["is_4k"] = bool(req.get("is4k") or media.get("is4k"))
    gw = connectors.get_gateway()
    if gw is not None:
        try:
            ctx["pending_count"] = await gw.jellyseerr.pending_count(ev["user_name"])
        except Exception:  # noqa: BLE001
            pass
        try:
            disks = await gw.radarr.diskspace()
            if disks:
                ctx["disk_free_gb"] = min(d.get("freeSpace", 0) for d in disks) / (1024 ** 3)
        except Exception:  # noqa: BLE001
            pass
    return ctx


async def process_pending(limit: int = 100) -> dict:
    """Decide any pending Jellyseerr requests not yet decided. Idempotent via the
    unique index on event_id. Returns per-action counts + enforcement info."""
    p = db.pool()
    if p is None:
        return {"ok": False, "error": "no db"}
    rules = _rules()
    counts = {"scanned": 0, "approve": 0, "manual": 0, "deny": 0, "enforced": 0, "enforce_failed": 0}
    try:
        async with p.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM hub_events e
                   WHERE e.source='jellyseerr' AND lower(e.event_type)='media_pending'
                     AND NOT EXISTS (SELECT 1 FROM policy_decisions d WHERE d.event_id = e.id)
                   ORDER BY e.id LIMIT $1""", limit)
            counts["scanned"] = len(rows)
            for ev in rows:
                try:
                    payload = ev["payload"] if isinstance(ev["payload"], dict) else json.loads(ev["payload"])
                except Exception:  # noqa: BLE001
                    payload = {}
                request_id = str((payload.get("request") or {}).get("request_id") or "") or None
                ctx = await _gather_ctx(ev, payload)
                decision = policy_rules.evaluate(ctx, rules)
                counts[decision.action] = counts.get(decision.action, 0) + 1

                enforced, enforce_result = False, None
                if POLICY_ENFORCE and decision.action == "approve" and request_id:
                    try:
                        await connectors.get_gateway().jellyseerr.approve_request(request_id)
                        enforced, enforce_result = True, "approved"
                        counts["enforced"] += 1
                    except Exception as e:  # noqa: BLE001
                        enforce_result = f"enforce failed: {e}"
                        counts["enforce_failed"] += 1
                        log.warning("policy enforce failed for request %s: %r", request_id, e)

                try:
                    await conn.execute(
                        """INSERT INTO policy_decisions
                               (event_id, request_id, requested_by, media_type, tmdb_id, title,
                                action, reasons, enforced, enforce_result)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                           ON CONFLICT (event_id) DO NOTHING""",
                        ev["id"], request_id, ev["user_name"], ev["media_type"], ev["tmdb_id"],
                        ev["title"], decision.action, "; ".join(decision.reasons),
                        enforced, enforce_result)
                except Exception as e:  # noqa: BLE001
                    log.warning("policy decision store failed for event %s: %r", ev["id"], e)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), **counts}
    return {"ok": True, "enforce": POLICY_ENFORCE, **counts}


async def _fetch(sql, *args):
    p = db.pool()
    if p is None:
        return False, []
    try:
        async with p.acquire() as c:
            return True, await c.fetch(sql, *args)
    except Exception:  # noqa: BLE001
        return False, []


@router.post("/evaluate-pending")
async def evaluate_pending(limit: int = 100):
    limit = max(1, min(limit, 500))
    result = await process_pending(limit)
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


@router.get("/decisions")
async def decisions(limit: int = 100):
    limit = max(1, min(limit, 500))
    ok, rows = await _fetch(
        "SELECT * FROM policy_decisions ORDER BY decided_at DESC LIMIT $1", limit)
    return {"ok": ok, "decisions": [
        {"decided_at": r["decided_at"].isoformat(), "action": r["action"],
         "title": r["title"], "media_type": r["media_type"], "requested_by": r["requested_by"],
         "reasons": r["reasons"], "enforced": r["enforced"], "enforce_result": r["enforce_result"]}
        for r in rows
    ]}


@router.get("/config")
async def config():
    r = _rules()
    return {"ok": True, "enforce": POLICY_ENFORCE, "rules": {
        "max_pending_per_user": r.max_pending_per_user, "min_disk_free_gb": r.min_disk_free_gb,
        "auto_approve_media_types": list(r.auto_approve_media_types),
        "deny_media_types": list(r.deny_media_types), "allow_4k": r.allow_4k,
    }}
