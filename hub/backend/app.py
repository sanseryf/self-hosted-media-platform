"""
Hub — the public front door and home for SQL/data projects.

Serves a forest-themed landing page (tile grid + live library dashboard) at the
apex, and provides an async Postgres layer that data-project routers plug into.

Endpoints
  GET /                 -> landing page (static/index.html)
  GET /api/health       -> {ok, db}
  GET /api/stats/*      -> summary / growth / recent / storage / status (landing)
  GET /api/audit/*      -> file-audit summary / duplicates / empties
  POST /api/events/ingest/{source} -> webhook receiver (idempotent, shared-secret;
                            the one API surface outside the session gate — see below)
  GET /api/events/*     -> event inbox observability: recent / dead / stats (gated)
  GET /api/gateway/*    -> L2 connector gateway: health / locate (typed reads over
                            Jellyfin + Sonarr + Radarr + Jellyseerr)
  */api/lifecycle/*     -> L3 lifecycle tracker: titles / timeline / stalled / stats /
                            drain (per-title request->available state machine)
  */api/reconcile/*     -> L3 three-way reconciler: run / last / findings (drift between
                            Jellyfin, *arr, and disk-proxy)
  */api/policy/*        -> L3 Jellyseerr auto-approver: evaluate-pending / decisions /
                            config (SHADOW MODE unless POLICY_ENFORCE=true)
  GET /api/watching/*   -> now-playing (live) / watch time by genre
  GET /api/library/*    -> browse/search/detail catalog (no auth, no playback)
  */api/auth/*          -> real per-user Jellyfin login/session (Phase 2a, no playback)
  */api/playback/*      -> in-browser video playback (Phase 2b, session-gated)
  */api/together/*      -> watch-together rooms: host/join/leave + SSE roster sync
                            (session-gated, in-memory registry - see together.py)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path


def _load_env_local():
    """Local-dev only: populate os.environ from ../.env.local (repo root of
    this stack, one level up from backend/) for keys not already set. Never
    touches the real .env - that one (once it exists) holds live production
    secrets and is only ever read by `docker compose --env-file`. Doesn't
    override anything already in the environment, so production containers
    (which get real env vars from docker-compose first) are unaffected."""
    path = Path(__file__).resolve().parent.parent / ".env.local"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_local()

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import connectors
import db
import stats
from projects import (audit, auth, discover, events, gateway, library, lifecycle,
                      login_guard, playback, policy, prefs, reconcile, together,
                      watching)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await auth.ensure_schema()
    await login_guard.ensure_schema()                              # /api/auth/login brute-force guard
    await prefs.ensure_schema()                                    # per-user preferences (watch-together opt-in)
    await events.ensure_schema()                                   # L1 event inbox / dead-letter
    await connectors.init_gateway()                                # L2 typed connector gateway
    await lifecycle.ensure_schema()                                # L3 lifecycle + transitions
    await reconcile.ensure_schema()                                # L3 reconciler runs + findings
    await policy.ensure_schema()                                   # L3 policy decision log
    sampler = asyncio.create_task(stats.status_sampler())          # records uptime history
    room_sweeper = asyncio.create_task(together.sweep_idle_rooms())  # reaps abandoned watch-together rooms
    drainer = asyncio.create_task(lifecycle.processor_loop())      # drains L1 inbox -> advances lifecycle
    login_pruner = asyncio.create_task(login_guard.prune_loop())   # ages out old login-attempt rows
    try:
        yield
    finally:
        sampler.cancel()
        room_sweeper.cancel()
        drainer.cancel()
        login_pruner.cancel()
        await connectors.close_gateway()
        await db.close()


app = FastAPI(title="Hub", version="1.2", lifespan=lifespan)


# ── Transfer compression ────────────────────────────────────────────────────
# index.html alone is ~262 KB of inline CSS+JS and hls.min.js is ~405 KB; both
# compress ~70%, so a cold homepage+player load drops from ~667 KB to ~197 KB.
# minimum_size=1000 skips the tiny JSON bodies most /api/* routes return, where
# the gzip header would cost more than it saves.
#
# SAFE FOR SSE: Starlette's GZipMiddleware carries
# DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream",), so the two live
# streams here — /api/watching/stream (the wall) and /api/together/stream/{code}
# (watch-together sync) — are passed through uncompressed and unbuffered. That
# exclusion is the ONLY reason blanket gzip is safe on this app; a Starlette
# downgrade below 0.45 (where it was added) would silently start buffering both
# streams and break live sync. Pinned in requirements.txt accordingly.
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── Security headers ────────────────────────────────────────────────────────
# Applied to every response, including static and error paths. The CSP is
# deliberately compatible with how this app is actually built rather than
# aspirational: index.html uses inline <style>/<script> and inline on* handlers
# throughout, so 'unsafe-inline' is required until that markup changes — the
# split into /static/app.css + /static/app.js removes the big blocks but the
# inline event handlers remain. img-src allows data: for the inline SVG favicon,
# blob: for hls.js media, and image.tmdb.org for the request rail's posters
# (discover.py hands the browser TMDB poster URLs directly). media-src and
# connect-src allow the Jellyfin public origin because the <video> element
# streams directly from it (see playback.py's "token-in-URL" note). frame-ancestors 'none' is the real
# clickjacking control; X-Frame-Options is the legacy fallback for it.
_JF_PUBLIC = os.environ.get("JELLYFIN_PUBLIC_URL", "").strip().rstrip("/")
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https://image.tmdb.org; "
    f"media-src 'self' blob: {_JF_PUBLIC}".rstrip() + "; "
    f"connect-src 'self' {_JF_PUBLIC}".rstrip() + "; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
}


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


# ── Site lockdown ───────────────────────────────────────────────────────────
# The landing page is a login gate: no media is visible until you sign in.
# Frontend hiding alone is trivially bypassed (curl /api/library/items), so the
# real lock lives here — every /api/* route requires a valid session. The only
# public API surface is auth (needed to log in and to check session state),
# health (liveness probes), and the webhook ingest sub-prefix. The index HTML
# and its /static assets stay public: they ARE the gate and carry no media data
# — every poster, title, and rail arrives only through these now-gated APIs.
#
# /api/events/ingest/ is exempted because the senders (Sonarr/Radarr/Jellyfin/
# Jellyseerr containers) cannot hold a Hub session — they self-authenticate with
# a shared secret (EVENTS_TOKEN) inside events.py instead. Only the *ingest*
# sub-prefix is public; the /api/events/ observability reads (recent/dead/stats)
# stay behind the session gate like everything else.
_PUBLIC_API_PREFIXES = ("/api/auth/", "/api/events/ingest/")
_PUBLIC_API_EXACT = {"/api/health"}


@app.middleware("http")
async def require_session_for_api(request, call_next):
    path = request.url.path
    if (path.startswith("/api/")
            and path not in _PUBLIC_API_EXACT
            and not path.startswith(_PUBLIC_API_PREFIXES)):
        if not await auth.has_valid_session(request):
            return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    return await call_next(request)


# GET+HEAD, not just GET: uptime monitors and container healthchecks routinely
# probe with HEAD, and unlike bare Starlette, FastAPI does NOT auto-add HEAD to
# a GET route — so both of these used to answer a monitor's HEAD with a 405.
@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health():
    return {"ok": True, "db": await db.health()}


app.include_router(stats.router, prefix="/api/stats")
app.include_router(audit.router, prefix="/api/audit")
app.include_router(events.router, prefix="/api/events")
app.include_router(gateway.router, prefix="/api/gateway")
app.include_router(lifecycle.router, prefix="/api/lifecycle")
app.include_router(reconcile.router, prefix="/api/reconcile")
app.include_router(policy.router, prefix="/api/policy")
app.include_router(watching.router, prefix="/api/watching")
app.include_router(discover.router, prefix="/api/request")
app.include_router(library.router, prefix="/api/library")
app.include_router(auth.router, prefix="/api/auth")
app.include_router(prefs.router, prefix="/api/prefs")
app.include_router(playback.router, prefix="/api/playback")
app.include_router(together.router, prefix="/api/together")


# ── Asset versioning ────────────────────────────────────────────────────────
# index.html used to be one 263 KB file with all its CSS and JS inline, so
# "cache the page" and "cache the code" were the same decision — and since the
# page must revalidate on every deploy, none of it could ever be cached hard.
# Now that app.css/app.js are separate files, they can be: each <link>/<script>
# URL carries ?v=<content hash>, which changes only when that file's bytes do.
#
# The hash is computed once per file per process and cached against the file's
# mtime, so this costs one read at first request and nothing afterwards, while
# still picking up an edit during local --reload development.
_ASSET_REFS = ("/static/tokens.css", "/static/app.css", "/static/app.js")
_asset_versions: dict[str, tuple[float, str]] = {}


def _asset_version(rel_url: str) -> str:
    """Short content hash for a /static/ asset, or "0" if unreadable."""
    path = STATIC_DIR / rel_url.removeprefix("/static/")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "0"
    cached = _asset_versions.get(rel_url)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"
    _asset_versions[rel_url] = (mtime, digest)
    return digest


@app.api_route("/", methods=["GET", "HEAD"])
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"ok": True, "msg": "Hub up. Landing page not built."})
    html = idx.read_text(encoding="utf-8")
    for ref in _ASSET_REFS:
        html = html.replace(f'"{ref}"', f'"{ref}?v={_asset_version(ref)}"')
    # The HTML itself still always revalidates: it changes every deploy, and
    # it's now only ~23 KB (~7 KB gzipped), so there's nothing to gain by
    # caching it and a stale shell to lose.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


class _VersionedStatic(StaticFiles):
    """Static files with cache lifetime decided by whether the URL is versioned.

    A request carrying ?v=<hash> can be cached forever: the URL changes the
    moment the file's contents do, so a stale response is impossible. Anything
    requested WITHOUT a version (wall.html/journey.html/request.html link
    tokens.css plainly, and direct hits exist) falls back to revalidate-always,
    which is the only safe default for a URL whose meaning can change.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned
            else "public, max-age=0, must-revalidate"
        )
        return response


if STATIC_DIR.exists():
    app.mount("/static", _VersionedStatic(directory=str(STATIC_DIR)), name="static")
