"""L3 — the lifecycle tracker: drains L1's event inbox and advances a per-title
state machine, correlating events across all four systems by shared external id.

    (background)                      drain unprocessed hub_events -> advance lifecycle
    GET  /api/lifecycle/titles        tracked titles + current state + stall flag
    GET  /api/lifecycle/title/{id}    one title + its full transition timeline
    GET  /api/lifecycle/stalled       titles stuck too long in a non-terminal state
    GET  /api/lifecycle/stats         counts by state
    POST /api/lifecycle/drain         process the inbox now (for demos/tests)

The brains (what each event does, identity stitching, out-of-order handling) live
in the pure, unit-tested lifecycle_state.py. This module is the thin DB executor
plus the read API. Processing is decoupled from ingest on purpose: L1 lands events
durably and cheaply; L3 drains them here, which is what finally gives L1's
attempts/dead-letter machinery teeth (a row that can't be processed
max_attempts times is flipped to 'dead').

Enrichment via the L2 gateway (locate() to backfill ids from live upstreams) is
best-effort and OFF by default — set LIFECYCLE_ENRICH=true to enable. It's wrapped
so a slow/dead upstream can never block the drain.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import connectors
import db
import lifecycle_state as L

router = APIRouter()
log = logging.getLogger("hub.lifecycle")

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "lifecycle_schema.sql"

MAX_ATTEMPTS = 5
ENRICH = os.environ.get("LIFECYCLE_ENRICH", "false").strip().lower() in ("1", "true", "yes")

# How long a title may sit in a given non-terminal state before we call it stalled.
STALL_HOURS = {"requested": 48, "grabbed": 24, "downloading": 24, "downloaded": 6}

_ID_FIELDS = ("tmdb_id", "imdb_id", "tvdb_id")


async def ensure_schema() -> None:
    p = db.pool()
    if p is None:
        return
    try:
        async with p.acquire() as conn:
            await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass


# ── the core: turn one hub_events row into a lifecycle transition ──────────────
def _event_to_dict(ev) -> dict:
    """Shape a hub_events row into the dict lifecycle_state.plan() expects."""
    return {
        "source": ev["source"], "event_type": ev["event_type"],
        "tmdb_id": ev["tmdb_id"], "imdb_id": ev["imdb_id"], "tvdb_id": ev["tvdb_id"],
        "media_type": ev["media_type"], "title": ev["title"], "requested_by": ev["user_name"],
        # upstream event time when we have it, else the ingest time — never null
        "at": ev["occurred_at"] or ev["received_at"],
    }


async def _find_current(conn, ev: dict):
    """Identity stitch: the existing title matching ANY of this event's ids, of a
    compatible media_type. Logs (a known limitation) when an event bridges more
    than one row — we take the earliest and leave the rest un-merged for now."""
    mt = ev.get("media_type")
    rows = await conn.fetch(
        """SELECT * FROM lifecycle
           WHERE (media_type IS NULL OR $1::text IS NULL OR media_type = $1)
             AND ( ($2::text IS NOT NULL AND tmdb_id = $2)
                OR ($3::text IS NOT NULL AND imdb_id = $3)
                OR ($4::text IS NOT NULL AND tvdb_id = $4) )
           ORDER BY id LIMIT 2""",
        mt, ev.get("tmdb_id"), ev.get("imdb_id"), ev.get("tvdb_id"),
    )
    if len(rows) > 1:
        log.warning("lifecycle: event bridges %d rows (ids tmdb=%s imdb=%s tvdb=%s) — "
                    "taking earliest; row-merge is a known TODO",
                    len(rows), ev.get("tmdb_id"), ev.get("imdb_id"), ev.get("tvdb_id"))
    return dict(rows[0]) if rows else None


async def _apply(conn, ev_row) -> str:
    """Advance the lifecycle for one event row, within the caller's transaction.
    Returns the Decision.action taken (for counting)."""
    ev = _event_to_dict(ev_row)

    if ENRICH and (gw := connectors.get_gateway()) is not None and L.target_state(ev["source"], ev["event_type"]):
        try:  # best-effort id backfill from live upstreams — never blocks the drain
            views = await gw.locate(tmdb=ev["tmdb_id"], imdb=ev["imdb_id"],
                                    tvdb=ev["tvdb_id"], media_type=ev["media_type"])
            for ref in views.values():
                if ref is None:
                    continue
                for k in _ID_FIELDS:
                    if not ev.get(k) and getattr(ref, k, None):
                        ev[k] = getattr(ref, k)
        except Exception as e:  # noqa: BLE001
            log.debug("enrich skipped for event %s: %r", ev_row["id"], e)

    current = await _find_current(conn, ev)
    d = L.plan(current, ev)

    if d.action == "none":
        return "none"

    if d.action == "create":
        new_id = await conn.fetchval(
            """INSERT INTO lifecycle
                   (media_type, tmdb_id, imdb_id, tvdb_id, title, state, state_since,
                    requested_by, first_seen_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now(),now()) RETURNING id""",
            ev.get("media_type"), d.merged_ids.get("tmdb_id"), d.merged_ids.get("imdb_id"),
            d.merged_ids.get("tvdb_id"), d.title, d.new_state, d.at, d.requested_by,
        )
        await _record_transition(conn, new_id, d, ev_row["id"])
        return "create"

    lid = current["id"]
    if d.action == "advance":
        await conn.execute(
            """UPDATE lifecycle SET state=$2, state_since=$3,
                   tmdb_id=COALESCE(tmdb_id,$4), imdb_id=COALESCE(imdb_id,$5),
                   tvdb_id=COALESCE(tvdb_id,$6), title=COALESCE(title,$7),
                   requested_by=COALESCE(requested_by,$8), updated_at=now()
               WHERE id=$1""",
            lid, d.new_state, d.at, d.merged_ids.get("tmdb_id"), d.merged_ids.get("imdb_id"),
            d.merged_ids.get("tvdb_id"), d.title, d.requested_by,
        )
        await _record_transition(conn, lid, d, ev_row["id"])
        return "advance"

    # merge_only: backfill ids/requester, no state change, no transition row
    await conn.execute(
        """UPDATE lifecycle SET
               tmdb_id=COALESCE(tmdb_id,$2), imdb_id=COALESCE(imdb_id,$3),
               tvdb_id=COALESCE(tvdb_id,$4),
               requested_by=COALESCE(requested_by,$5), updated_at=now()
           WHERE id=$1""",
        lid, d.merged_ids.get("tmdb_id"), d.merged_ids.get("imdb_id"),
        d.merged_ids.get("tvdb_id"), d.requested_by,
    )
    return "merge_only"


async def _record_transition(conn, lifecycle_id: int, d: L.Decision, event_id: int) -> None:
    await conn.execute(
        """INSERT INTO lifecycle_transitions
               (lifecycle_id, from_state, to_state, source, event_id, event_type, at)
           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        lifecycle_id, d.from_state, d.new_state, d.source, event_id, d.event_type, d.at,
    )


async def process_pending(limit: int = 200) -> dict:
    """Drain up to `limit` un-processed inbox events, advancing the lifecycle.
    Each event is handled in its own transaction so one poison row can't roll back
    the batch. On repeated failure a row is flipped to 'dead' (L1's DLQ). Returns
    per-action counts. Never raises — a whole-batch failure returns an error dict."""
    p = db.pool()
    if p is None:
        return {"ok": False, "error": "no db"}
    counts = {"scanned": 0, "create": 0, "advance": 0, "merge_only": 0, "none": 0,
              "processed": 0, "dead": 0, "failed": 0}
    try:
        async with p.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM hub_events WHERE processed_at IS NULL AND status='received' "
                "ORDER BY id LIMIT $1", limit)
            counts["scanned"] = len(rows)
            for ev in rows:
                try:
                    async with conn.transaction():
                        action = await _apply(conn, ev)
                        await conn.execute(
                            "UPDATE hub_events SET status='processed', processed_at=now(), "
                            "attempts=attempts+1 WHERE id=$1", ev["id"])
                    counts[action] = counts.get(action, 0) + 1
                    counts["processed"] += 1
                except Exception as e:  # noqa: BLE001 — isolate a poison event
                    counts["failed"] += 1
                    log.warning("lifecycle: event %s failed: %r", ev["id"], e)
                    # separate txn so the failure bookkeeping itself commits
                    try:
                        async with conn.transaction():
                            newd = await conn.fetchval(
                                "UPDATE hub_events SET attempts=attempts+1, last_error=$2, "
                                "status=CASE WHEN attempts+1 >= $3 THEN 'dead' ELSE status END "
                                "WHERE id=$1 RETURNING status", ev["id"], str(e)[:500], MAX_ATTEMPTS)
                        if newd == "dead":
                            counts["dead"] += 1
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), **counts}
    return {"ok": True, **counts}


async def processor_loop(interval: float = 30.0) -> None:
    """Background drain, started in app.py's lifespan like stats.status_sampler().
    Polls the inbox every `interval`s; a failing drain is logged and retried next
    tick, never crashing the loop."""
    while True:
        try:
            res = await process_pending()
            if res.get("processed") or res.get("dead"):
                log.info("lifecycle drain: %s", {k: res[k] for k in
                         ("scanned", "create", "advance", "processed", "dead") if res.get(k)})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("lifecycle processor_loop error: %r", e)
        await asyncio.sleep(interval)


# ── read API ──────────────────────────────────────────────────────────────────
def _stall_hours(state: str, age_hours: float) -> tuple[bool, int | None]:
    thr = STALL_HOURS.get(state)
    if thr is None:
        return False, None
    return age_hours > thr, thr


async def _fetch(sql, *args):
    p = db.pool()
    if p is None:
        return False, []
    try:
        async with p.acquire() as c:
            return True, await c.fetch(sql, *args)
    except Exception:  # noqa: BLE001
        return False, []


def _title_row(r) -> dict:
    age_h = None
    if r["state_since"] is not None and r["age_seconds"] is not None:
        age_h = float(r["age_seconds"]) / 3600.0
    stalled, thr = _stall_hours(r["state"], age_h) if age_h is not None else (False, None)
    return {
        "id": r["id"], "title": r["title"], "media_type": r["media_type"], "state": r["state"],
        "ids": {"tmdb": r["tmdb_id"], "imdb": r["imdb_id"], "tvdb": r["tvdb_id"]},
        "requested_by": r["requested_by"],
        "state_since": r["state_since"].isoformat() if r["state_since"] else None,
        "hours_in_state": round(age_h, 1) if age_h is not None else None,
        "stalled": stalled, "stall_threshold_h": thr,
    }


_SELECT_TITLES = ("SELECT *, EXTRACT(EPOCH FROM (now()-state_since)) AS age_seconds "
                  "FROM lifecycle")


@router.get("/titles")
async def titles(state: str | None = None, limit: int = 100):
    limit = max(1, min(limit, 500))
    if state:
        ok, rows = await _fetch(_SELECT_TITLES + " WHERE state=$1 ORDER BY updated_at DESC LIMIT $2",
                                state, limit)
    else:
        ok, rows = await _fetch(_SELECT_TITLES + " ORDER BY updated_at DESC LIMIT $1", limit)
    return {"ok": ok, "titles": [_title_row(r) for r in rows]}


@router.get("/stalled")
async def stalled(limit: int = 100):
    """Titles that have sat in a non-terminal state past its threshold. Computed
    in SQL so it's cheap: age > threshold, per-state."""
    limit = max(1, min(limit, 500))
    # Build a VALUES list of (state, hours) to join against — keeps thresholds in one place (Python).
    pairs = ", ".join(f"('{s}', {h})" for s, h in STALL_HOURS.items())
    ok, rows = await _fetch(
        _SELECT_TITLES + f" JOIN (VALUES {pairs}) AS t(st, hrs) ON t.st = lifecycle.state "
        "WHERE now() - state_since > make_interval(hours => t.hrs) "
        "ORDER BY state_since ASC LIMIT $1", limit)
    return {"ok": ok, "titles": [_title_row(r) for r in rows]}


@router.get("/title/{lifecycle_id}")
async def title_detail(lifecycle_id: int):
    ok, rows = await _fetch(_SELECT_TITLES + " WHERE id=$1", lifecycle_id)
    if not ok or not rows:
        return {"ok": False, "title": None, "timeline": []}
    ok2, trans = await _fetch(
        "SELECT from_state, to_state, source, event_type, event_id, at "
        "FROM lifecycle_transitions WHERE lifecycle_id=$1 ORDER BY at ASC, id ASC", lifecycle_id)
    return {"ok": True, "title": _title_row(rows[0]), "timeline": [
        {"from": t["from_state"], "to": t["to_state"], "source": t["source"],
         "event_type": t["event_type"], "event_id": t["event_id"],
         "at": t["at"].isoformat() if t["at"] else None} for t in trans
    ]}


@router.get("/stats")
async def stats():
    ok, rows = await _fetch("SELECT state, count(*) AS n FROM lifecycle GROUP BY state")
    by_state = {r["state"]: int(r["n"]) for r in rows}
    return {"ok": ok, "by_state": by_state, "total": sum(by_state.values())}


@router.post("/drain")
async def drain(limit: int = 200):
    """Manually run the processor once. Session-gated (it's under /api/, not
    /api/events/ingest/), so only a logged-in admin can trigger it."""
    limit = max(1, min(limit, 1000))
    result = await process_pending(limit)
    code = 200 if result.get("ok") else 503
    return JSONResponse(result, status_code=code)
