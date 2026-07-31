"""L3 — three-way reconciler: does Jellyfin, the *arr apps, and (proxied) disk agree?

    POST /api/reconcile/run       enumerate + diff + store a run   (session-gated)
    GET  /api/reconcile/last      the latest run's summary
    GET  /api/reconcile/findings  findings for a run (?run_id, ?kind, ?limit)

No background loop — a full-library enumeration is heavy, so it's an explicit
trigger (cron it, or run from the console). The pure set-diff is reconcile_diff.py;
this module enumerates via the L2 gateway and persists results.

Critical guard: if any source fails to enumerate, we DO NOT diff — an empty list
from a failed Jellyfin call would report the entire *arr library as "missing from
Jellyfin". Such a run is stored ok=false with the error, and produces no findings.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import connectors
import db
import reconcile_diff

router = APIRouter()
log = logging.getLogger("hub.reconcile")

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "reconcile_schema.sql"
_KINDS = ("orphan_in_jellyfin", "missing_from_jellyfin", "arr_wanted")


async def ensure_schema() -> None:
    p = db.pool()
    if p is None:
        return
    try:
        async with p.acquire() as conn:
            await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass


async def run_reconcile() -> dict:
    gw = connectors.get_gateway()
    if gw is None:
        return {"ok": False, "error": "gateway not initialized"}
    p = db.pool()
    if p is None:
        return {"ok": False, "error": "no db"}

    snap = await gw.snapshot()
    jf, arr = snap["jellyfin"], snap["arr"]
    if not (jf["ok"] and arr["ok"]):
        # Store the failure so the history shows it, but never diff partial data.
        note = f"jellyfin={jf['error'] or 'ok'} arr={arr['error'] or 'ok'}"
        try:
            async with p.acquire() as conn:
                await conn.execute(
                    "INSERT INTO reconcile_runs (ok, note, jellyfin_items, arr_items) "
                    "VALUES (false, $1, $2, $3)", note, len(jf["items"]), len(arr["items"]))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "enumeration incomplete — refusing to diff partial data",
                "detail": note}

    result = reconcile_diff.diff(jf["items"], arr["items"])
    s = result["summary"]
    try:
        async with p.acquire() as conn:
            async with conn.transaction():
                run_id = await conn.fetchval(
                    """INSERT INTO reconcile_runs
                           (ok, jellyfin_items, arr_items, orphan_in_jellyfin,
                            missing_from_jellyfin, arr_wanted)
                       VALUES (true,$1,$2,$3,$4,$5) RETURNING id""",
                    s["jellyfin_items"], s["arr_items"], s["orphan_in_jellyfin"],
                    s["missing_from_jellyfin"], s["arr_wanted"])
                for kind in _KINDS:
                    for f in result[kind]:
                        ids = f["ids"]
                        await conn.execute(
                            """INSERT INTO reconcile_findings
                                   (run_id, kind, title, media_type, tmdb_id, imdb_id, tvdb_id)
                               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                            run_id, kind, f["title"], f["media_type"],
                            ids.get("tmdb_id"), ids.get("imdb_id"), ids.get("tvdb_id"))
    except Exception as e:  # noqa: BLE001
        log.warning("reconcile store failed: %r", e)
        return {"ok": False, "error": "store failed", "summary": s}
    return {"ok": True, "run_id": run_id, "summary": s}


async def _fetch(sql, *args):
    p = db.pool()
    if p is None:
        return False, []
    try:
        async with p.acquire() as c:
            return True, await c.fetch(sql, *args)
    except Exception:  # noqa: BLE001
        return False, []


@router.post("/run")
async def run():
    result = await run_reconcile()
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


@router.get("/last")
async def last():
    ok, rows = await _fetch("SELECT * FROM reconcile_runs ORDER BY ran_at DESC LIMIT 1")
    if not ok or not rows:
        return {"ok": ok, "run": None}
    r = rows[0]
    return {"ok": True, "run": {
        "id": r["id"], "ran_at": r["ran_at"].isoformat(), "healthy": r["ok"], "note": r["note"],
        "summary": {k: r[k] for k in ("jellyfin_items", "arr_items", "orphan_in_jellyfin",
                                      "missing_from_jellyfin", "arr_wanted")},
    }}


@router.get("/findings")
async def findings(run_id: int | None = None, kind: str | None = None, limit: int = 200):
    limit = max(1, min(limit, 1000))
    if run_id is None:
        ok, rows = await _fetch("SELECT id FROM reconcile_runs WHERE ok ORDER BY ran_at DESC LIMIT 1")
        if not ok or not rows:
            return {"ok": ok, "run_id": None, "findings": []}
        run_id = rows[0]["id"]
    if kind and kind in _KINDS:
        ok, rows = await _fetch(
            "SELECT * FROM reconcile_findings WHERE run_id=$1 AND kind=$2 ORDER BY title LIMIT $3",
            run_id, kind, limit)
    else:
        ok, rows = await _fetch(
            "SELECT * FROM reconcile_findings WHERE run_id=$1 ORDER BY kind, title LIMIT $2",
            run_id, limit)
    return {"ok": ok, "run_id": run_id, "findings": [
        {"kind": r["kind"], "title": r["title"], "media_type": r["media_type"],
         "ids": {"tmdb": r["tmdb_id"], "imdb": r["imdb_id"], "tvdb": r["tvdb_id"]}}
        for r in rows
    ]}
