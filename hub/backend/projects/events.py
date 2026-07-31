"""L1 — event ingestion: an idempotent webhook receiver for the four upstreams
that feed the integration platform (Jellyfin, Sonarr, Radarr, Jellyseerr).

    POST /api/events/ingest/{source}   receive one webhook            (shared-secret auth)
    GET  /api/events/recent            latest events                  (session-gated)
    GET  /api/events/stats             counts by source / type / status (session-gated)
    GET  /api/events/dead              dead-letter queue              (session-gated)

Design notes (also the portfolio talking points):

  • Idempotent. These upstreams emit no stable event id and retry on our 5xx, so
    at-least-once delivery is a given. We derive a deterministic dedup key from
    the canonical event tuple (event_sources.py) and lean on a UNIQUE constraint +
    ON CONFLICT DO NOTHING — a duplicate delivery is a no-op, never a double count.

  • Self-authenticating. Ingest can't sit behind Hub's session gate (a Sonarr
    container can't log in), so /ingest/* is exempted from that gate in app.py and
    instead checks a shared secret (EVENTS_TOKEN) in constant time. The read/
    observability endpoints keep the normal session gate.

  • Status codes are the retry contract. Normally Hub degrades to a JSON error
    rather than 500 — but here a 5xx is a feature: DB down → 503 so a well-behaved
    upstream retries later instead of dropping the event. Conversely an
    unparseable/unknown payload → 202 (accepted, dead-lettered) so it is NOT
    retried, because retrying it can't help.

  • Dead-letter, don't discard. A payload we can't parse or recognize is still
    persisted (status='dead', with the error) so nothing is ever silently lost.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import db
import event_sources

router = APIRouter()
log = logging.getLogger("hub.events")

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "events_schema.sql"

# Shared secret webhooks must present (header X-Hub-Token or ?token=). Unset means
# "open" — fine for local dev, but production's .env MUST set it, since /ingest is
# deliberately outside the session gate. Warned about, loudly, on an open ingest.
EVENTS_TOKEN = os.environ.get("EVENTS_TOKEN", "").strip()
_warned_open = False

# Full column list for the observability reads, kept in one place.
_COLS = ("id, source, event_type, status, imdb_id, tmdb_id, tvdb_id, title, "
         "media_type, user_name, occurred_at, received_at, last_error")


async def ensure_schema() -> None:
    """Apply events_schema.sql at boot. Non-fatal, mirroring auth.ensure_schema():
    if the DB is down, ingest just 503s (correctly telling upstreams to retry)
    until it's up — it never blocks Hub from booting."""
    p = db.pool()
    if p is None:
        return
    try:
        async with p.acquire() as conn:
            await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass


def _auth_ok(request: Request) -> bool:
    """Constant-time shared-secret check. Accepts the token from the X-Hub-Token
    header (preferred) or a ?token= query param (fallback for senders that only
    let you set a URL). An unset EVENTS_TOKEN means open — allowed, but warned."""
    global _warned_open
    if not EVENTS_TOKEN:
        if not _warned_open:
            log.warning("EVENTS_TOKEN is unset — /api/events/ingest is UNAUTHENTICATED. "
                        "Set EVENTS_TOKEN in production.")
            _warned_open = True
        return True
    presented = request.headers.get("X-Hub-Token") or request.query_params.get("token") or ""
    return hmac.compare_digest(presented, EVENTS_TOKEN)


async def _dead_letter(p, source: str, raw: bytes, error: str, payload: dict | None):
    """Persist an unparseable/unrecognized event instead of dropping it. Returns
    202: we accepted and stored it, but it is NOT processed — and a 202 (rather
    than a 5xx) tells the upstream not to bother retrying, since a payload we
    can't parse won't parse any better next time."""
    body = payload if payload is not None else {
        "_unparseable_body": raw.decode("utf-8", "replace")[:10000]
    }
    # Hash the raw bytes so identical redeliveries of the same bad payload collapse
    # to one dead-letter row rather than piling up.
    dedup = f"dead:{source}:{hashlib.sha256(raw).hexdigest()}"
    et = str((payload or {}).get("NotificationType")
             or (payload or {}).get("eventType")
             or (payload or {}).get("notification_type") or "unknown")
    try:
        async with p.acquire() as c:
            await c.execute(
                """INSERT INTO hub_events
                       (source, event_type, dedup_key, status, attempts, last_error, payload)
                   VALUES ($1, $2, $3, 'dead', 1, $4, $5::jsonb)
                   ON CONFLICT (dedup_key) DO NOTHING""",
                source, et, dedup, error, json.dumps(body),
            )
    except Exception:  # noqa: BLE001 — never let dead-lettering itself 500
        log.warning("failed to persist dead-letter for %s: %s", source, error)
    return JSONResponse({"ok": False, "dead_letter": True, "error": error}, status_code=202)


@router.post("/ingest/{source}")
async def ingest(source: str, request: Request):
    source = source.lower()
    if source not in event_sources.SOURCES:
        return JSONResponse({"ok": False, "error": f"unknown source '{source}'"}, status_code=404)
    if not _auth_ok(request):
        return JSONResponse({"ok": False, "error": "bad or missing token"}, status_code=401)

    raw = await request.body()

    p = db.pool()
    if p is None:
        # Deliberate 5xx: storage is down, so tell the upstream to retry rather
        # than acking an event we can't durably record.
        return JSONResponse({"ok": False, "error": "storage unavailable"}, status_code=503)

    # Parse — anything that isn't a JSON object is dead-lettered, not lost.
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except Exception as e:  # noqa: BLE001
        return await _dead_letter(p, source, raw, f"unparseable JSON: {e}", None)

    # Normalize — an unrecognized shape is dead-lettered with the reason.
    try:
        env = event_sources.extract(source, payload)
    except event_sources.UnknownEvent as e:
        return await _dead_letter(p, source, raw, f"unrecognized event: {e}", payload)
    except Exception as e:  # noqa: BLE001 — a genuinely broken payload, not our bug
        return await _dead_letter(p, source, raw, f"extract error: {e}", payload)

    # Idempotent insert: a redelivery hits the UNIQUE(dedup_key) and no-ops.
    try:
        async with p.acquire() as c:
            row = await c.fetchrow(
                """INSERT INTO hub_events
                       (source, event_type, dedup_key, imdb_id, tmdb_id, tvdb_id,
                        title, media_type, user_name, occurred_at, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
                   ON CONFLICT (dedup_key) DO NOTHING
                   RETURNING id""",
                env.source, env.event_type, env.dedup_key, env.imdb_id, env.tmdb_id,
                env.tvdb_id, env.title, env.media_type, env.user_name, env.occurred_at,
                json.dumps(payload),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("ingest insert failed for %s/%s: %r", source, env.event_type, e)
        return JSONResponse({"ok": False, "error": "storage error"}, status_code=503)

    if row is None:
        return {"ok": True, "deduped": True, "event_type": env.event_type}
    return {"ok": True, "deduped": False, "id": row["id"], "event_type": env.event_type}


# ───────────────────────────────────────── observability (session-gated) ──────
async def _fetch(sql: str, *args):
    """Run a read query, returning (ok, rows). Never raises — a fresh DB without
    the table yet comes back ok:false with an empty payload, like audit.py."""
    p = db.pool()
    if p is None:
        return False, []
    try:
        async with p.acquire() as c:
            rows = await c.fetch(sql, *args)
        return True, rows
    except Exception:  # noqa: BLE001
        return False, []


def _iso(v):
    return v.isoformat() if v is not None else None


def _row(r) -> dict:
    return {
        "id": r["id"], "source": r["source"], "event_type": r["event_type"],
        "status": r["status"], "title": r["title"], "media_type": r["media_type"],
        "user_name": r["user_name"],
        "ids": {"imdb": r["imdb_id"], "tmdb": r["tmdb_id"], "tvdb": r["tvdb_id"]},
        "occurred_at": _iso(r["occurred_at"]), "received_at": _iso(r["received_at"]),
        "last_error": r["last_error"],
    }


@router.get("/recent")
async def recent(limit: int = 50, source: str | None = None):
    limit = max(1, min(limit, 200))
    if source:
        ok, rows = await _fetch(
            f"SELECT {_COLS} FROM hub_events WHERE source=$1 "
            f"ORDER BY received_at DESC LIMIT $2", source.lower(), limit)
    else:
        ok, rows = await _fetch(
            f"SELECT {_COLS} FROM hub_events ORDER BY received_at DESC LIMIT $1", limit)
    return {"ok": ok, "events": [_row(r) for r in rows]}


@router.get("/dead")
async def dead(limit: int = 50):
    limit = max(1, min(limit, 200))
    ok, rows = await _fetch(
        f"SELECT {_COLS} FROM hub_events_dead ORDER BY received_at DESC LIMIT $1", limit)
    return {"ok": ok, "events": [_row(r) for r in rows]}


@router.get("/stats")
async def stats():
    """A compact health snapshot for the status page: totals, a dead count, and a
    per-source-per-status-per-type breakdown of the last 24h."""
    ok, rows = await _fetch(
        "SELECT source, status, event_type, count(*) AS n "
        "FROM hub_events WHERE received_at > now() - interval '24 hours' "
        "GROUP BY source, status, event_type ORDER BY n DESC")
    ok2, totals = await _fetch(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE status='dead') AS dead, "
        "count(*) FILTER (WHERE received_at > now() - interval '24 hours') AS last_24h "
        "FROM hub_events")
    t = totals[0] if totals else None
    return {
        "ok": ok and ok2,
        "total": int(t["total"]) if t else 0,
        "dead": int(t["dead"]) if t else 0,
        "last_24h": int(t["last_24h"]) if t else 0,
        "breakdown": [
            {"source": r["source"], "status": r["status"],
             "event_type": r["event_type"], "count": int(r["n"])}
            for r in rows
        ],
    }
