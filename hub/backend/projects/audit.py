"""File-audit read endpoints — back the maintenance view / console.

    GET /api/audit/summary     per-type titles, dup groups, wasted bytes, empties
    GET /api/audit/duplicates  duplicate title groups (report only)
    GET /api/audit/empties     empty folders (auto-purged by audit_collect.py)
    GET /api/audit/last-run    latest audit_runs row

All read from tables/functions populated by audit_collect.py. Every handler
degrades gracefully: if the pool is down or the functions/tables don't exist
yet (fresh DB), it returns ok:false with an empty payload instead of a 500.
"""
from fastapi import APIRouter

import db

router = APIRouter()


async def _fetch(sql, *args):
    """Run a query, returning (ok, rows). Never raises."""
    p = db.pool()
    if p is None:
        return False, []
    try:
        async with p.acquire() as c:
            rows = await c.fetch(sql, *args)
        return True, rows
    except Exception:  # noqa: BLE001 — missing function/table on a fresh DB, etc.
        return False, []


@router.get("/summary")
async def summary():
    ok, rows = await _fetch("SELECT * FROM fn_audit_summary()")
    return {"ok": ok, "rows": [
        {
            "media_type": r["media_type"],
            "titles": int(r["titles"]),
            "dup_groups": int(r["dup_groups"]),
            "dup_wasted_bytes": int(r["dup_wasted_bytes"]),
            "empty_dirs": int(r["empty_dirs"]),
        } for r in rows
    ]}


@router.get("/duplicates")
async def duplicates(limit: int = 100):
    limit = max(1, min(limit, 500))
    ok, rows = await _fetch("SELECT * FROM fn_duplicate_titles() LIMIT $1", limit)
    return {"ok": ok, "groups": [
        {
            "media_type": r["media_type"],
            "name": r["name_norm"],
            "copies": int(r["copies"]),
            "keeper": r["keeper_path"],
            "paths": list(r["paths"]),
            "wasted_bytes": int(r["wasted_bytes"]),
        } for r in rows
    ]}


@router.get("/empties")
async def empties(limit: int = 200):
    limit = max(1, min(limit, 1000))
    ok, rows = await _fetch("SELECT * FROM fn_empty_folders() LIMIT $1", limit)
    return {"ok": ok, "folders": [
        {"path": r["path"], "media_type": r["media_type"], "depth": int(r["depth"])}
        for r in rows
    ]}


@router.get("/last-run")
async def last_run():
    ok, rows = await _fetch("SELECT * FROM audit_runs ORDER BY ran_at DESC LIMIT 1")
    if not ok:
        return {"ok": False, "run": None}
    if not rows:
        return {"ok": True, "run": None}
    r = rows[0]
    return {"ok": True, "run": {
        "ran_at": r["ran_at"].isoformat(),
        "dirs_scanned": r["dirs_scanned"],
        "titles": r["titles"],
        "dup_groups": r["dup_groups"],
        "empties_found": r["empties_found"],
        "empties_purged": r["empties_purged"],
        "wasted_bytes": int(r["wasted_bytes"]),
    }}
