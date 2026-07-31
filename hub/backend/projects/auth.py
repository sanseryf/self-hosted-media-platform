"""Real per-user Jellyfin login for Hub.

    POST /api/auth/login    {username, password} -> mints a session cookie
    GET  /api/auth/me       -> {ok, user: {username} | null}
    POST /api/auth/logout   -> clears the session

Phase 2a only: proves a visitor can be identified as themselves against
Jellyfin ("Logged in as {username}"). No playback here — see
get_session_jellyfin_token() at the bottom, the explicit handoff point for
Phase 2b's PlaybackInfo/streaming work.

Session model: an opaque random session_id cookie (httponly), backed by the
`hub_sessions` Postgres table mapping it to the real Jellyfin per-user token
server-side. The Jellyfin token — and the Jellyfin user id — must NEVER
appear in any response body. Every route here follows the rest of Hub's
degrade-never-500 idiom: DB down or Jellyfin unreachable both come back as a
graceful JSON error, never an unhandled 500.
"""
from __future__ import annotations

import os
import secrets
import time
import urllib.error
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
from projects import login_guard
from projects.watching import _jf_post_async

router = APIRouter()

log = logging.getLogger("hub.auth")

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "auth_schema.sql"

COOKIE_NAME = "hub_session"
SESSION_DAYS = 30

# Production is HTTPS at the browser regardless of the internal NPM->container
# hop being plain HTTP, so Secure=true is correct and required there. Local
# dev (http://localhost:8090) needs HUB_COOKIE_SECURE=false in hub/.env.local.
HUB_COOKIE_SECURE = os.environ.get("HUB_COOKIE_SECURE", "true").strip().lower() not in (
    "false", "0", "no", "",
)

# Safety gate specific to /login: every other Hub route that talks to
# Jellyfin (watching.py, library.py) also gates on JELLYFIN_API_KEY being set,
# so leaving that unset in hub/.env.local is enough to keep local dev from
# ever reaching real Jellyfin. /login can't use that same gate — real
# end-user login legitimately needs no admin key at all, only the visitor's
# own username/password — so without a separate switch it would silently
# reach production Jellyfin (JELLYFIN_URL's own fallback default is the real
# production LAN address) from any machine that can route to it, using
# whatever credentials someone happens to type into the local dev UI. This
# flag closes that gap: default false/unset means /login always short-circuits
# before _jf_post_async is ever called, no network call happens at all.
# Production's real .env will need HUB_JELLYFIN_LOGIN_ENABLED=true for login
# to actually work there — that's a deliberate, separate deploy-time decision,
# not something this code (or this session) sets.
HUB_JELLYFIN_LOGIN_ENABLED = os.environ.get("HUB_JELLYFIN_LOGIN_ENABLED", "false").strip().lower() in (
    "true", "1", "yes",
)

_EMBY_AUTH_HEADER = (
    'MediaBrowser Client="Hub", Device="Hub Web", DeviceId="hub-server", Version="1.2"'
)


async def ensure_schema() -> None:
    """Apply auth_schema.sql at boot. Non-fatal: mirrors db.connect()'s
    philosophy — if the DB is down, login just 503s until it's up, never
    blocks hub from booting."""
    p = db.pool()
    if p is None:
        return
    try:
        async with p.acquire() as conn:
            await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass


class LoginBody(BaseModel):
    username: str
    password: str


# ── In-process session cache ────────────────────────────────────────────────
# Every gated /api/* request used to cost its own SELECT: app.py's middleware
# calls has_valid_session() on all of them, and one real homepage load fires
# 15+ in parallel (rails, stats, posters, library, trending). The previous
# answer to that was to widen the pool to max_size=10 (see db.py) — this is the
# cheaper one: the same session_id resolved once, then served from memory for a
# few seconds, turning that 15-query burst into roughly one.
#
# Why a plain dict is enough: hub runs one container, one uvicorn worker (no
# --workers in the Dockerfile, no replicas in compose) — the same single-process
# assumption together.py's room registry already documents and depends on. If
# Hub ever gets a second worker this stays *correct* (each process just does its
# own lookups) but the invalidation below becomes per-process — flagged, not
# assumed away.
#
# The cost of caching is staleness, bounded deliberately:
#   * A session revoked in Postgres by hand stays usable for up to
#     _SESSION_TTL seconds. Logout does NOT rely on that expiring — it evicts
#     the entry directly (see logout()), so the common case is instant.
#   * Negative results are cached far more briefly. They're cached at all so a
#     flood of requests bearing one junk cookie can't turn into a flood of
#     SELECTs, but short enough that a genuine sign-in isn't shadowed by a
#     just-missed lookup.
_SESSION_TTL = 30.0
_SESSION_NEG_TTL = 5.0
_SESSION_CACHE_MAX = 2000
# session_id -> (expires_at_monotonic, row_or_None)
_session_cache: dict[str, tuple[float, object]] = {}


def _cache_get(session_id: str):
    """Returns (hit, row). `hit` distinguishes "cached as invalid" (hit=True,
    row=None) from "not cached at all" (hit=False, row=None)."""
    entry = _session_cache.get(session_id)
    if entry is None:
        return False, None
    expires, row = entry
    if time.monotonic() >= expires:
        _session_cache.pop(session_id, None)
        return False, None
    return True, row


def _cache_put(session_id: str, row) -> None:
    if len(_session_cache) >= _SESSION_CACHE_MAX:
        # Cheap bounded eviction: drop whatever is already past its TTL, and if
        # that frees nothing, drop the oldest inserted key. Sessions are small
        # and short-lived here, so an exact LRU would be more bookkeeping than
        # the problem is worth.
        now = time.monotonic()
        for key in [k for k, (exp, _) in _session_cache.items() if exp <= now]:
            _session_cache.pop(key, None)
        if len(_session_cache) >= _SESSION_CACHE_MAX:
            _session_cache.pop(next(iter(_session_cache)), None)
    ttl = _SESSION_TTL if row is not None else _SESSION_NEG_TTL
    _session_cache[session_id] = (time.monotonic() + ttl, row)


def _cache_evict(session_id: str) -> None:
    _session_cache.pop(session_id, None)


async def _lookup_session(session_id: str):
    """The one place a session_id is resolved to its row, cache in front.

    Consolidates what used to be two near-identical queries (has_valid_session
    selected last_seen_at; _get_session_row selected the identity columns) into
    a single SELECT that fetches everything either caller needs — so the gate
    and the playback/identity paths now share one cache entry instead of
    competing for the pool with two different statements.

    Keeps both properties the split versions were careful about:

    1. Resilience to a single flaky connection. asyncpg pools don't
       health-check a connection before handing it out, so one that went stale
       between requests (recycled server-side, a brief network blip) surfaces
       as an *exception* on its next use — and a single flaky acquire, on any
       one of a page's 15+ concurrent gated requests, was previously enough to
       401 that request and read to the browser as "the whole page is broken."
       One bounded retry on a fresh acquire turns that into a non-event; the
       short acquire timeout keeps a genuinely-stuck request failing fast
       (clean 401) rather than hanging the page load.
    2. Sliding renewal. Any gated traffic — not just /me polling and playback —
       slides expires_at forward, so a visitor who only browses keeps their own
       session alive. Still throttled: the UPDATE only fires when last_seen_at
       is over 5 minutes stale, and now additionally only on a cache *miss*, so
       the write rate drops further without changing the semantics.

    Returns the row, or None for no DB / expired / persistent error — every
    caller treats None as "not logged in"."""
    hit, cached = _cache_get(session_id)
    if hit:
        return cached

    p = db.pool()
    if p is None:
        return None
    for attempt in (1, 2):
        try:
            async with p.acquire(timeout=3) as conn:
                row = await conn.fetchrow(
                    "SELECT jellyfin_user_id, jellyfin_username, jellyfin_token, last_seen_at "
                    "FROM hub_sessions WHERE session_id=$1 AND expires_at > now()",
                    session_id,
                )
                if row is not None and (
                    datetime.now(timezone.utc) - row["last_seen_at"] > timedelta(minutes=5)
                ):
                    await conn.execute(
                        "UPDATE hub_sessions SET last_seen_at=now(), "
                        "expires_at = CASE WHEN now() - last_seen_at > interval '1 hour' "
                        "THEN now() + make_interval(days => $2) ELSE expires_at END "
                        "WHERE session_id=$1",
                        session_id, SESSION_DAYS,
                    )
            # DIAGNOSTIC: a cookie WAS presented but no live row matched it —
            # the actual "logged-in but 401'd" case. Distinct from the (silent,
            # expected) no-cookie signed-out path in the callers above.
            if row is None:
                log.warning("session lookup: cookie present but no valid row (sid=%s…)", session_id[:8])
            _cache_put(session_id, row)
            return row
        except Exception as e:  # noqa: BLE001
            log.warning("session lookup: DB error attempt %d (sid=%s…): %r", attempt, session_id[:8], e)
            if attempt == 2:
                return None
            continue
    return None


async def _get_session_row(request: Request):
    """Look up the caller's session row (if any, and not expired). Backs /me,
    playback (get_session_jellyfin_token), and watch-together
    (get_session_identity). Returns None on no cookie, no DB, or a persistent
    error — callers treat that identically to "not logged in"."""
    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id:
        return None
    return await _lookup_session(session_id)


async def has_valid_session(request: Request) -> bool:
    """Session-validity check for the site-wide API gate (app.py's middleware)
    — runs on *every* /api/* request, so it is the single hottest call in the
    app. Shares _lookup_session()'s cache with the identity/playback paths, so
    a page load that fires 15+ gated requests resolves the session once.

    Returns False on no cookie, no DB, expired row, or persistent error — all
    "not logged in"."""
    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id:
        return False
    return await _lookup_session(session_id) is not None


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=HUB_COOKIE_SECURE,
        path="/",
    )


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    # Checked first, before touching body/username/password or making any
    # network call — see HUB_JELLYFIN_LOGIN_ENABLED's definition above for why
    # this exists. Must stay the very first thing this handler does.
    if not HUB_JELLYFIN_LOGIN_ENABLED:
        return JSONResponse(
            {"ok": False, "error": "Jellyfin login is disabled in this environment"},
            status_code=503,
        )

    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        return JSONResponse(
            {"ok": False, "error": "Username and password are required"}, status_code=400
        )

    # Brute-force guard (login_guard.py). Runs before the Jellyfin round-trip so
    # a throttled attacker costs us a single indexed COUNT and never touches
    # Jellyfin at all. The 429 body deliberately says nothing about whether the
    # username exists — same wording no matter which of the two budgets tripped.
    ip = login_guard.client_ip(request)
    retry_after = await login_guard.check(ip, username)
    if retry_after is not None:
        log.warning("login throttled (ip=%s user=%s retry_after=%ss)", ip, username, retry_after)
        return JSONResponse(
            {"ok": False, "error": "Too many sign-in attempts. Try again in a few minutes."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    try:
        data = await _jf_post_async(
            "/Users/AuthenticateByName",
            {"Username": username, "Pw": password},
            headers={"X-Emby-Authorization": _EMBY_AUTH_HEADER},
        )
    except urllib.error.HTTPError as e:
        # Jellyfin is reachable and rejected the credentials. This — and the
        # malformed-response case below — are the ONLY outcomes recorded as a
        # failure: they're the only ones where a real credential check actually
        # happened and said no.
        if e.code in (401, 403):
            await login_guard.record(ip, username, succeeded=False)
            return JSONResponse(
                {"ok": False, "error": "Invalid username or password"}, status_code=401
            )
        # Jellyfin itself is broken/unreachable. Deliberately NOT recorded: no
        # credential was ever verified, and counting an outage against members
        # would turn a Jellyfin restart into a site-wide lockout.
        return JSONResponse(
            {"ok": False, "error": "Jellyfin is unreachable"}, status_code=503
        )
    except Exception:  # noqa: BLE001 — URLError, timeout, DNS failure, etc.
        return JSONResponse(
            {"ok": False, "error": "Jellyfin is unreachable"}, status_code=503
        )

    user = data.get("User") or {}
    jf_user_id = user.get("Id")
    jf_username = user.get("Name") or username
    jf_token = data.get("AccessToken")
    if not jf_user_id or not jf_token:
        await login_guard.record(ip, username, succeeded=False)
        return JSONResponse(
            {"ok": False, "error": "Invalid username or password"}, status_code=401
        )

    p = db.pool()
    if p is None:
        return JSONResponse(
            {"ok": False, "error": "Session storage unavailable"}, status_code=503
        )

    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    try:
        async with p.acquire() as conn:
            await conn.execute(
                "INSERT INTO hub_sessions "
                "(session_id, jellyfin_user_id, jellyfin_username, jellyfin_token, expires_at) "
                "VALUES ($1,$2,$3,$4,$5)",
                session_id, jf_user_id, jf_username, jf_token, expires_at,
            )
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": "Session storage unavailable"}, status_code=503
        )

    _set_session_cookie(response, session_id)
    # Clears this username's outstanding failures as a side effect, so four
    # fumbled attempts followed by a correct one leaves a clean slate rather
    # than one strike from a lockout.
    await login_guard.record(ip, username, succeeded=True)
    # NEVER include jf_token or jf_user_id here — only the display username.
    return {"ok": True, "user": {"username": jf_username}}


@router.get("/me")
async def me(request: Request):
    row = await _get_session_row(request)
    if row is None:
        return {"ok": True, "user": None}
    return {"ok": True, "user": {"username": row["jellyfin_username"]}}


@router.post("/logout")
async def logout(request: Request, response: Response):
    session_id = request.cookies.get(COOKIE_NAME)
    if session_id:
        # Evict FIRST, and unconditionally. Sign-out has to be immediate — if
        # this only deleted the row, the in-process cache would keep answering
        # "still valid" for up to _SESSION_TTL seconds afterwards. Done before
        # the DELETE (rather than after) so it still happens even if the DB
        # call below throws.
        _cache_evict(session_id)
        p = db.pool()
        if p is not None:
            try:
                async with p.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM hub_sessions WHERE session_id=$1", session_id
                    )
            except Exception:  # noqa: BLE001
                pass
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


async def get_session_jellyfin_token(request: Request) -> tuple[str, str] | None:
    """Phase 2b handoff point (not used yet). Looks up the session cookie,
    returns (jellyfin_user_id, jellyfin_token) or None. Phase 2b's playback
    router imports this exactly the way library.py imports
    _jf_get_async/_item_to_card from watching.py, and uses the returned
    per-user token as the per-request Jellyfin credential for
    PlaybackInfo/streaming calls."""
    row = await _get_session_row(request)
    if row is None:
        return None
    return row["jellyfin_user_id"], row["jellyfin_token"]


async def get_session_identity(request: Request) -> tuple[str, str] | None:
    """Watch-together's handoff point (together.py). Returns (session_id,
    jellyfin_username) or None. Unlike get_session_jellyfin_token(), this
    never touches Jellyfin at all — a room's roster/host-check only needs to
    know *who* is calling (the raw hub_session cookie value doubles as a
    stable per-participant key, since a session already maps 1:1 to a
    signed-in person) and what name to show for them. Deliberately does not
    hand back the Jellyfin token/user id: together.py has no Jellyfin
    credential surface at all, by design — actual playback stays exactly
    where Phase 2b already put it, on each participant's own
    /api/playback/* calls using their own session."""
    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id:
        return None
    row = await _get_session_row(request)
    if row is None:
        return None
    return session_id, row["jellyfin_username"]


async def find_active_session_by_username(username: str) -> tuple[str, str] | None:
    """Watch-together hot-join's reverse lookup (together.py): given a
    Jellyfin display username - the same public-facing value already shown
    everywhere (participant chips, Now Playing's own "who" field), never
    the Jellyfin user id/token - finds that person's most-recently-active
    hub session, if they currently have one. Returns (session_id,
    jellyfin_username), same shape as get_session_identity(), or None if
    nobody matching is currently signed into Hub right now (on Jellyfin
    directly instead of through Hub, or their hub session has lapsed).
    Kept here rather than in together.py so `hub_sessions` stays read/
    written from exactly one place, matching every other query in this
    file - together.py has no direct DB access of its own by design."""
    username = (username or "").strip()
    if not username:
        return None
    p = db.pool()
    if p is None:
        return None
    try:
        async with p.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT session_id, jellyfin_username FROM hub_sessions "
                "WHERE jellyfin_username=$1 AND expires_at > now() "
                "ORDER BY last_seen_at DESC LIMIT 1",
                username,
            )
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return row["session_id"], row["jellyfin_username"]
