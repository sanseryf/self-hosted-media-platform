"""Per-user Hub preferences.

    GET  /api/prefs/me         -> {ok, allow_hot_join}   (the caller's own prefs)
    POST /api/prefs/hot-join   {enabled: bool}           -> persist allow_hot_join

Session-gated like the rest of Hub — identity comes from the same session
cookie every other user-scoped route uses, via auth.get_session_identity(),
so the username a preference is written under is always the caller's own and
can never be spoofed by the request body.

This module owns the `hub_user_prefs` table outright — the read helpers below
(get_allow_hot_join / get_allowed_hot_join_users) are the ONLY way watching.py
and together.py touch it, mirroring how auth.py is the single owner of
`hub_sessions`. Both helpers fail *closed* (default to "not allowed") on any DB
trouble: the preference gates a privacy affordance, so the safe degrade is to
withhold it, never to expose a solo watcher who never opted in.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db

# NOTE: get_session_identity is imported lazily inside the two route handlers,
# NOT at module top. auth.py imports watching.py, watching.py imports this
# module (for now()'s joinable gate), so a top-level `from projects.auth import
# ...` here closes a circular import at boot (auth -> watching -> prefs ->
# auth-still-initializing). The read helpers below need no auth at all; only
# the handlers do, and by then auth is fully loaded and cached in sys.modules.

log = logging.getLogger("hub.prefs")

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "prefs_schema.sql"

router = APIRouter()


async def ensure_schema() -> None:
    """Apply prefs_schema.sql at boot. Non-fatal, exactly like
    auth.ensure_schema(): a DB that's down just means every preference reads
    as its default until it's back, never blocks hub from booting."""
    p = db.pool()
    if p is None:
        return
    try:
        async with p.acquire() as conn:
            await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass


async def get_allow_hot_join(username: str) -> bool:
    """Has this person opted in to being hot-joined? False on no row, no DB,
    or any error — the privacy-preserving default (see module docstring)."""
    username = (username or "").strip()
    if not username:
        return False
    p = db.pool()
    if p is None:
        return False
    try:
        async with p.acquire(timeout=3) as conn:
            row = await conn.fetchrow(
                "SELECT allow_hot_join FROM hub_user_prefs WHERE jellyfin_username=$1",
                username,
            )
        return bool(row and row["allow_hot_join"])
    except Exception:  # noqa: BLE001
        return False


async def get_allowed_hot_join_users(usernames: list[str]) -> set[str]:
    """Bulk variant for now() — one round-trip for a whole Now Playing
    snapshot rather than one per session. Returns the subset of `usernames`
    that have opted in; anyone not in the returned set (including on a DB
    error, when the empty set is returned) is treated as not-joinable."""
    names = sorted({(u or "").strip() for u in usernames if (u or "").strip()})
    if not names:
        return set()
    p = db.pool()
    if p is None:
        return set()
    try:
        async with p.acquire(timeout=3) as conn:
            rows = await conn.fetch(
                "SELECT jellyfin_username FROM hub_user_prefs "
                "WHERE jellyfin_username = ANY($1) AND allow_hot_join = true",
                names,
            )
        return {r["jellyfin_username"] for r in rows}
    except Exception:  # noqa: BLE001
        return set()


async def _set_allow_hot_join(username: str, value: bool) -> bool:
    """Upsert one person's allow_hot_join. Returns True on success."""
    username = (username or "").strip()
    if not username:
        return False
    p = db.pool()
    if p is None:
        return False
    try:
        async with p.acquire(timeout=3) as conn:
            await conn.execute(
                "INSERT INTO hub_user_prefs (jellyfin_username, allow_hot_join, updated_at) "
                "VALUES ($1, $2, now()) "
                "ON CONFLICT (jellyfin_username) DO UPDATE "
                "SET allow_hot_join = EXCLUDED.allow_hot_join, updated_at = now()",
                username, bool(value),
            )
        return True
    except Exception:  # noqa: BLE001
        return False


def _unauthorized() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)


class HotJoinPrefBody(BaseModel):
    enabled: bool


@router.get("/me")
async def prefs_me(request: Request):
    """The caller's own preferences. 401 with no session; otherwise always
    ok:true with every preference resolved to a concrete value (defaults
    filled in) so the client never has to special-case a missing field."""
    from projects.auth import get_session_identity  # lazy — see module top
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    _session_id, username = identity
    return {"ok": True, "allow_hot_join": await get_allow_hot_join(username)}


@router.post("/hot-join")
async def set_hot_join(body: HotJoinPrefBody, request: Request):
    """Turn "let others watch with me" on or off for the caller."""
    from projects.auth import get_session_identity  # lazy — see module top
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    _session_id, username = identity
    if not await _set_allow_hot_join(username, body.enabled):
        return {"ok": False, "error": "Could not save that preference right now.",
                "allow_hot_join": await get_allow_hot_join(username)}
    return {"ok": True, "allow_hot_join": bool(body.enabled)}
