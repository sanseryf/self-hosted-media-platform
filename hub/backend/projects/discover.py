"""Discovery endpoints backed by Jellyseerr — "popular to request" for the landing.

GET /api/request/trending   currently-popular movies/series you DON'T already have,
                            each deep-linking into Jellyseerr to request (auth stays there).

Read-only proxy: the Jellyseerr key stays server-side; posters come from TMDB's public
image CDN (no key). Degrades gracefully — never a 500 — if Jellyseerr isn't configured.
"""
import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from projects import auth

router = APIRouter()

JS_URL = os.environ.get("JELLYSEERR_URL", "http://10.0.0.10:5055").rstrip("/")
JS_KEY = os.environ.get("JELLYSEERR_API_KEY", "")
JS_PUBLIC = os.environ.get("JELLYSEERR_PUBLIC_URL", "https://request.example.org").rstrip("/")
# Optional: an internal book-request service (not part of this repo) that the
# unified palette can search and request through, server-side on the LAN, so
# books and audiobooks are findable without the browser leaving Hub. Unset ->
# the book rows simply never appear.
BF_URL = os.environ.get("BOOK_REQUEST_URL", "").rstrip("/")
IMG = "https://image.tmdb.org/t/p/w342"

# Jellyseerr/Overseerr mediaInfo.status enum
_STATUS = {1: "none", 2: "pending", 3: "processing", 4: "partial", 5: "available"}


def _js_get(path, **params):
    url = f"{JS_URL}/api/v1{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-Api-Key": JS_KEY})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def _js_post(path, body):
    """POST to Jellyseerr (request submission). Blocking urllib; callers wrap in
    asyncio.to_thread. Lets urllib.error.HTTPError propagate so the caller can
    branch on the code (409 = already requested)."""
    url = f"{JS_URL}/api/v1{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"X-Api-Key": JS_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _shape(it):
    mt = it.get("mediaType")
    if mt not in ("movie", "tv"):
        return None
    status = _STATUS.get((it.get("mediaInfo") or {}).get("status"), "none")
    poster = it.get("posterPath")
    date = it.get("releaseDate") or it.get("firstAirDate") or ""
    return {
        "id": it.get("id"),
        "type": mt,
        "title": it.get("title") or it.get("name"),
        "year": (date[:4] if date else None),
        "poster": (IMG + poster) if poster else None,
        "overview": (it.get("overview") or "").strip(),
        "status": status,                       # none | pending | processing | partial
        "url": f"{JS_PUBLIC}/{mt}/{it.get('id')}",
    }


@router.get("/trending")
async def trending(limit: int = 16):
    """Popular titles you don't already have — for the landing 'request anything' rail."""
    if not JS_KEY:
        return {"ok": False, "items": []}
    limit = max(1, min(limit, 30))
    try:
        # _js_get is blocking (urllib) - hop to a thread so a slow/unreachable
        # Jellyseerr doesn't stall the single event loop for every other request.
        data = await asyncio.to_thread(_js_get, "/discover/trending", page=1, language="en")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "items": []}
    out = []
    for it in (data.get("results") or []):
        row = _shape(it)
        if not row or row["status"] == "available" or not row["poster"]:
            continue                            # skip what we already have / has no art
        out.append(row)
        if len(out) >= limit:
            break
    return {"ok": True, "items": out}


# ── per-user attribution ──────────────────────────────────────────────────────
# Requests submitted through Hub use the server-side admin key, but we attribute
# each one to the actual member by resolving their Jellyfin username -> Jellyseerr
# userId and passing it on the request. Without this every request would look like
# the admin made it and per-user quotas would be meaningless. The user list is
# small and changes rarely, so it's cached for a few minutes; a miss falls back to
# an unattributed request rather than failing.
_user_map: dict[str, int] = {}
_user_map_ts = 0.0
_user_map_lock = threading.Lock()
_USER_MAP_TTL = 300.0


def _load_user_map() -> dict[str, int]:
    m: dict[str, int] = {}
    skip = 0
    while True:
        data = _js_get("/user", take=100, skip=skip)
        results = data.get("results") or []
        for u in results:
            uid = u.get("id")
            if uid is None:
                continue
            for name in (u.get("jellyfinUsername"), u.get("username"),
                         u.get("plexUsername"), u.get("displayName")):
                if name:
                    m[name.lower()] = uid
        if len(results) < 100:
            break
        skip += 100
    return m


def _jellyseerr_user_id(username):
    """Resolve a Jellyfin username to its Jellyseerr userId (cached). Returns None
    if unknown — the caller then submits unattributed rather than failing."""
    if not username:
        return None
    global _user_map, _user_map_ts
    now = time.time()
    with _user_map_lock:
        stale = not _user_map or (now - _user_map_ts) > _USER_MAP_TTL
    if stale:
        try:
            fresh = _load_user_map()
            with _user_map_lock:
                _user_map, _user_map_ts = fresh, now
        except Exception:  # noqa: BLE001 — attribution is best-effort
            pass
    with _user_map_lock:
        return _user_map.get(username.lower())


@router.get("/search")
async def search(q: str, limit: int = 20):
    """Search everything Jellyseerr can request (movies + TV), each tagged with its
    current availability so the UI can show 'available / requested / request it'."""
    if not JS_KEY:
        return {"ok": False, "items": []}
    q = (q or "").strip()
    if len(q) < 2:
        return {"ok": True, "items": []}
    limit = max(1, min(limit, 40))
    try:
        data = await asyncio.to_thread(_js_get, "/search", query=q, page=1, language="en")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "items": []}
    out = []
    for it in (data.get("results") or []):
        row = _shape(it)                        # movie/tv only, poster + status
        if not row or not row["poster"]:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return {"ok": True, "items": out}


class _SubmitBody(BaseModel):
    tmdb_id: int
    media_type: str                             # movie | tv


@router.post("/submit")
async def submit(body: _SubmitBody, request: Request):
    """Submit a request to Jellyseerr on the signed-in member's behalf. The site
    gate guarantees a valid session here; we resolve that member to their
    Jellyseerr user so the request is attributed to them (quotas apply)."""
    if not JS_KEY:
        return JSONResponse({"ok": False, "error": "requests unavailable"}, status_code=503)
    mt = (body.media_type or "").lower()
    if mt not in ("movie", "tv"):
        return JSONResponse({"ok": False, "error": "media_type must be movie or tv"}, status_code=400)

    ident = await auth.get_session_identity(request)      # (session_id, username) | None
    username = ident[1] if ident else None
    user_id = await asyncio.to_thread(_jellyseerr_user_id, username) if username else None

    payload = {"mediaType": mt, "mediaId": body.tmdb_id}
    if mt == "tv":
        payload["seasons"] = "all"                        # request the whole show
    if user_id is not None:
        payload["userId"] = user_id                       # attribute to the real member

    try:
        res = await asyncio.to_thread(_js_post, "/request", payload)
    except urllib.error.HTTPError as e:
        if e.code == 409:                                 # already requested / exists
            return {"ok": True, "already": True, "message": "Already requested"}
        return JSONResponse({"ok": False, "error": f"Jellyseerr returned {e.code}"}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return {"ok": True, "request_id": res.get("id"),
            "attributed_to": username if user_id is not None else None}


# ── Book-request service proxy (books / comics / audiobooks) ───────────────────
def _bf_get(path, **params):
    url = f"{BF_URL}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def _bf_post(path, body):
    req = urllib.request.Request(f"{BF_URL}{path}", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


_BF_KINDS = ("all", "book", "audiobook", "manga", "comic")


@router.get("/books")
async def books_search(q: str, kind: str = "all", limit: int = 12):
    """Proxy the book-request service's search so the hub palette can find
    books, comics and audiobooks. Covers are dropped (palette rows are text-only)."""
    q = (q or "").strip()
    if len(q) < 2 or not BF_URL:          # no book service configured -> no book rows
        return {"ok": True, "items": []}
    if kind not in _BF_KINDS:
        kind = "all"
    limit = max(1, min(limit, 30))

    async def _fetch(k):
        try:
            d = await asyncio.to_thread(_bf_get, "/api/search", q=q, kind=k, limit=limit)
            return d.get("results") or []
        except Exception:  # noqa: BLE001 — one flaky kind shouldn't kill the rest
            return []

    # The service's "all" = book+manga+comic; audiobook is a SEPARATE kind there
    # (it reuses the book search, so folding it into "all" would duplicate every
    # book title). The unified palette wants both, so for kind=all we also pull
    # audiobooks and merge — otherwise audiobooks never appear in the palette.
    if kind == "all":
        main, audio = await asyncio.gather(_fetch("all"), _fetch("audiobook"))
        raw = main + audio
    else:
        raw = await _fetch(kind)

    items = [{
        "id": r.get("id"), "type": r.get("type"), "title": r.get("title"),
        "author": r.get("author") or r.get("subtitle") or "", "year": r.get("year") or "",
    } for r in raw if r.get("id")]
    return {"ok": True, "items": items}


class _BookReq(BaseModel):
    id: str
    type: str                                   # book | audiobook | manga | comic


@router.post("/books/submit")
async def books_submit(body: _BookReq):
    """Request a book/comic/audiobook via the book-request service, in place."""
    if not BF_URL:
        return JSONResponse({"ok": False, "error": "no book request service configured"}, status_code=503)
    t = (body.type or "").lower()
    if t not in ("book", "audiobook", "manga", "comic"):
        return JSONResponse({"ok": False, "error": "bad type"}, status_code=400)
    try:
        res = await asyncio.to_thread(_bf_post, "/api/request", {"type": t, "id": body.id})
    except urllib.error.HTTPError as e:
        return JSONResponse({"ok": False, "error": f"book service returned {e.code}"}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return {"ok": True, "message": (res.get("message") if isinstance(res, dict) else None) or "Requested"}
