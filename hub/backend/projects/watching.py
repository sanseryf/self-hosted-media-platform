"""Watch endpoints for the landing page.

    GET /api/watching/now        live now-playing from Jellyfin /Sessions
    GET /api/watching/by-genre   watch minutes per genre (kind=all|movies|tv)

/now hits Jellyfin directly (no DB). /by-genre reads the heartbeats that the
console's backfill (or watch_poll.py) accumulates via fn_watch_by_genre().
Both degrade gracefully: never a 500, even if the function/DB is missing.
"""
import asyncio
import itertools
import json
import os
import random
import threading
import urllib.parse
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

import db
from projects import prefs

router = APIRouter()

# Cadence of the SSE live wall (server-side Jellyfin poll interval, seconds).
STREAM_INTERVAL = float(os.environ.get("WALL_STREAM_SECONDS", "4"))

JF_URL = os.environ.get("JELLYFIN_URL", "http://10.0.0.10:8096").rstrip("/")
JF_KEY = os.environ.get("JELLYFIN_API_KEY", "")


def _jf_get(path, **params):
    params["api_key"] = JF_KEY
    url = f"{JF_URL}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())


async def _jf_get_async(path, **params):
    """asyncio.to_thread wrapper - _jf_get is blocking urllib, and with uvicorn's
    default single worker/single event loop, calling it directly from an async
    handler stalls every other in-flight request for the duration."""
    return await asyncio.to_thread(_jf_get, path, **params)


def _jf_post(path, body, headers=None, params=None):
    """params (optional) - query string args, e.g. UserId for PlaybackInfo /
    Sessions/Playing*. Kept as a distinct kwarg (not folded into `path`) so
    callers don't hand-build querystrings; None/empty means no query string,
    identical to the pre-Phase-2b behaviour every existing caller relies on."""
    url = f"{JF_URL}{path}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


async def _jf_post_async(path, body, headers=None, params=None):
    """asyncio.to_thread wrapper - see _jf_get_async's docstring for why."""
    return await asyncio.to_thread(_jf_post, path, body, headers, params)


@router.get("/now")
async def now():
    """Currently-playing sessions. Live, best-effort; empty if JF unreachable."""
    if not JF_KEY:
        return {"ok": False, "error": "no Jellyfin key", "sessions": []}
    try:
        sessions = await _jf_get_async("/Sessions")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "sessions": []}

    # Who, of the people currently playing, has opted in to being hot-joined?
    # One bulk lookup for the whole snapshot (not one per session). Anyone not
    # in this set — including on any DB trouble, when it comes back empty — is
    # marked joinable:false below, so the "Watch with {name}" CTA simply never
    # renders for them. This is the privacy default: hot-join is opt-in. See
    # prefs.py and together.py's hot_join() server-side re-check.
    playing_users = [s.get("UserName") for s in sessions if s.get("NowPlayingItem")]
    joinable_users = await prefs.get_allowed_hot_join_users(playing_users)

    out = []
    for s in sessions:
        item = s.get("NowPlayingItem")
        if not item:
            continue
        state = s.get("PlayState") or {}
        pos = state.get("PositionTicks") or 0
        total = item.get("RunTimeTicks") or 0
        genres = item.get("Genres") or []
        item_type = item.get("Type")  # Movie | Episode | Audio | ...
        is_episode = item_type == "Episode"
        # Anime = a TV episode tagged with the Anime genre in Jellyfin.
        is_anime = is_episode and any(g.lower() == "anime" for g in genres)
        # Normalised kind for the badge: anime > tv > movie > (raw type)
        if is_anime:
            kind = "anime"
        elif is_episode:
            kind = "tv"
        elif item_type == "Movie":
            kind = "movie"
        else:
            kind = (item_type or "other").lower()
        # poster: prefer the series poster for episodes, else the item's primary
        imgs = item.get("ImageTags") or {}
        if is_episode and item.get("SeriesPrimaryImageTag"):
            img_id, img_tag = item.get("SeriesId"), item.get("SeriesPrimaryImageTag")
        else:
            img_id, img_tag = item.get("Id"), imgs.get("Primary")
        out.append({
            "user": s.get("UserName"),
            # Opt-in gate for watch-together's hot-join CTA — the frontend only
            # offers "Watch with {name}" when this is true (see nowFields()).
            "joinable": s.get("UserName") in joinable_users,
            "type": item_type,
            "kind": kind,
            "title": item.get("Name"),
            "series": item.get("SeriesName"),
            # The actual playable item id (the episode's own id for TV, not
            # img_id above - that one's deliberately overloaded to the
            # *series* id whenever a series poster is being shown, which
            # makes it unsafe to reuse for anything that needs the real
            # thing-being-watched, like watch-together's hot-join). Not a
            # secret - item ids are already public throughout Hub's API.
            "item_id": item.get("Id"),
            "img_id": img_id,
            "img_tag": img_tag,
            "season": item.get("ParentIndexNumber"),   # None for movies
            "episode": item.get("IndexNumber"),         # None for movies
            "genres": genres,
            "is_anime": is_anime,
            "paused": bool(state.get("IsPaused")),
            "device": s.get("DeviceName"),
            "progress": round(pos / total, 3) if total else 0.0,
            "elapsed": int(pos / 1e7),                                # seconds watched
            "runtime": int(total / 1e7),                             # total seconds
            "remaining": max(0, int((total - pos) / 1e7)) if total else 0,
        })
    return {"ok": True, "sessions": out}


# ── Live-wall broadcaster ───────────────────────────────────────────────────
# ONE Jellyfin poll shared by every connected wall client, rather than a poll
# loop per connection.
#
# This is what the /stream docstring always claimed ("the server holds one
# Jellyfin poll loop and fans it out") but the code didn't do: gen() was defined
# inside the request handler, so each viewer got their own loop. With N people
# on the wall that was N calls to Jellyfin /Sessions *and* N prefs lookups every
# STREAM_INTERVAL seconds — load that scaled with the audience for data that is
# identical for all of them.
#
# The shape here is deliberately the same one together.py already uses for room
# events (a dict of per-subscriber asyncio.Queues, fanned out by a producer), so
# both SSE surfaces in this app now work the same way.
_wall_subscribers: dict[int, asyncio.Queue] = {}
_wall_seq = itertools.count()
_wall_poller: asyncio.Task | None = None
_wall_last: dict | None = None


async def _wall_poll_loop() -> None:
    """Single producer. Polls Jellyfin once per interval and pushes the snapshot
    to every subscriber. Exits on its own once the last subscriber leaves, so an
    idle server does no Jellyfin traffic at all."""
    global _wall_last, _wall_poller
    try:
        while _wall_subscribers:
            try:
                snap = await now()
            except Exception as e:  # noqa: BLE001 — one bad poll must not kill the wall
                snap = {"ok": False, "error": str(e), "sessions": []}
            _wall_last = snap
            for q in list(_wall_subscribers.values()):
                try:
                    q.put_nowait(snap)
                except asyncio.QueueFull:
                    # A client too slow to drain a 1-deep queue is already
                    # behind; dropping the frame is right — the next snapshot
                    # is a complete state, not a delta, so it self-heals.
                    pass
            await asyncio.sleep(STREAM_INTERVAL)
    finally:
        # Let the next subscriber start a fresh loop rather than leaving a
        # finished task parked in the global.
        _wall_poller = None


@router.get("/stream")
async def stream(request: Request):
    """SSE stream of now-playing snapshots for the live wall. Emits the same
    payload as /now every STREAM_INTERVAL seconds. Reuses now()'s shaping
    directly so there's one source of truth for the card shape, and shares one
    Jellyfin poll across all connected clients (see _wall_poll_loop). Ends
    cleanly when the client disconnects."""
    global _wall_poller
    key = next(_wall_seq)
    # maxsize=1: only the newest snapshot matters. A backed-up client should
    # resync to current state, not replay a queue of stale ones.
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    _wall_subscribers[key] = queue
    if _wall_poller is None or _wall_poller.done():
        _wall_poller = asyncio.create_task(_wall_poll_loop())

    async def gen():
        try:
            # First paint: hand over the most recent snapshot immediately so a
            # newly-opened wall isn't blank until the next poll tick.
            if _wall_last is not None:
                yield f"data: {json.dumps(_wall_last)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=STREAM_INTERVAL * 3)
                except asyncio.TimeoutError:
                    # Nothing to send, but prove the connection is alive and give
                    # the disconnect check above a chance to run.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(snap)}\n\n"
        finally:
            _wall_subscribers.pop(key, None)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


@router.get("/by-genre")
async def by_genre(kind: str = "all"):
    if kind not in ("all", "movies", "tv"):
        kind = "all"
    p = db.pool()
    if p is None:
        return {"ok": False, "kind": kind, "rows": []}
    try:
        async with p.acquire() as c:
            rows = await c.fetch("SELECT * FROM fn_watch_by_genre($1)", kind)
    except Exception:  # noqa: BLE001 — function not created yet, DB down, etc.
        return {"ok": False, "kind": kind, "rows": []}
    return {"ok": True, "kind": kind, "rows": [
        {
            "genre": r["genre"],
            "media_kind": r["media_kind"],
            "minutes": float(r["minutes"]),
            "plays": int(r["plays"]),
        } for r in rows
    ]}


def _item_to_card(it: dict) -> dict:
    """Shape a raw Jellyfin /Items entry into the card JSON used across the
    landing rail, the catalog grid, and the detail modal. Kept here (rather
    than in library.py) since latest() was the first caller; library.py
    imports this directly rather than duplicating the shaping logic."""
    tag = (it.get("ImageTags") or {}).get("Primary")
    # Only populated for callers that requested "Backdrop" in EnableImageTypes
    # (the hero rail) — harmless None for every existing caller that doesn't.
    backdrops = it.get("BackdropImageTags") or []
    prov = it.get("ProviderIds") or {}
    people = it.get("People") or []
    director = next((p.get("Name") for p in people if p.get("Type") == "Director"), None)
    cast = [p.get("Name") for p in people if p.get("Type") == "Actor"][:4]
    ticks = it.get("RunTimeTicks") or 0
    return {
        "id": it.get("Id"),
        "title": it.get("Name"),
        "type": it.get("Type"),               # Movie | Series
        "year": it.get("ProductionYear"),
        "overview": (it.get("Overview") or "").strip(),
        "genres": (it.get("Genres") or [])[:5],
        "tag": tag,
        "backdrop": backdrops[0] if backdrops else None,
        "community": it.get("CommunityRating"),   # IMDb-style 0–10
        "critic": it.get("CriticRating"),         # Rotten Tomatoes %
        "official": it.get("OfficialRating"),     # e.g. PG-13
        "runtime_min": round(ticks / 600000000) if ticks else None,
        "imdb": prov.get("Imdb"),
        "tmdb": prov.get("Tmdb"),
        "director": director,
        "cast": cast,
    }


@router.get("/latest")
async def latest(limit: int = 14):
    """Recently added movies/series with synopsis + poster tag (for the landing rail)."""
    if not JF_KEY:
        return {"ok": False, "items": []}
    limit = max(1, min(limit, 30))
    try:
        data = await _jf_get_async("/Items", SortBy="DateCreated", SortOrder="Descending",
                       Recursive="true", IncludeItemTypes="Movie,Series", Limit=limit,
                       Fields="Overview,Genres,ProductionYear,CommunityRating,"
                              "CriticRating,OfficialRating,ProviderIds,People,RunTimeTicks",
                       ImageTypeLimit=1, EnableImageTypes="Primary")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "items": []}
    items = [_item_to_card(it) for it in (data.get("Items") or [])]
    return {"ok": True, "items": items}


_HERO_FIELDS = (
    "Overview,Genres,ProductionYear,CommunityRating,"
    "CriticRating,OfficialRating,ProviderIds,People,RunTimeTicks"
)


@router.get("/hero")
async def hero(limit: int = 8):
    """A randomized mix of recently-added + random library titles, with
    backdrop art, for the homepage hero banner. Roughly half the slots go to
    the newest additions, half to a random pool from the rest of the
    library, then the whole set is shuffled so the split isn't visible as an
    order. Degrades to an empty list — never a 500."""
    if not JF_KEY:
        return {"ok": False, "items": []}
    limit = max(1, min(limit, 12))
    recent_n = max(1, (limit + 1) // 2)
    random_n = limit - recent_n
    try:
        recent, rand = await asyncio.gather(
            _jf_get_async("/Items", SortBy="DateCreated", SortOrder="Descending",
                          Recursive="true", IncludeItemTypes="Movie,Series", Limit=recent_n,
                          Fields=_HERO_FIELDS, ImageTypeLimit=1,
                          EnableImageTypes="Primary,Backdrop"),
            _jf_get_async("/Items", SortBy="Random", Recursive="true",
                          IncludeItemTypes="Movie,Series", Limit=max(random_n * 3, random_n),
                          Fields=_HERO_FIELDS, ImageTypeLimit=1,
                          EnableImageTypes="Primary,Backdrop"),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "items": []}

    seen: set[str] = set()
    out = []
    for it in (recent.get("Items") or []):
        iid = it.get("Id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        out.append(_item_to_card(it))

    pool = [it for it in (rand.get("Items") or []) if it.get("Id") not in seen]
    random.shuffle(pool)
    for it in pool[:random_n]:
        seen.add(it.get("Id"))
        out.append(_item_to_card(it))

    random.shuffle(out)
    return {"ok": True, "items": out}


# Server-side poster cache, keyed on (id, tag, h, kind) - posters/backdrops
# essentially never change for a given tag, so this cuts repeat Jellyfin
# round-trips across visitors/reloads instead of only benefiting a single
# browser's own local cache.
#
# Bounded on BYTES, not entry count. It was previously capped at 500 entries,
# which sounds bounded but isn't in any useful sense: this route serves both
# ~30 KB posters and backdrops requested at maxHeight=1600 quality=90, so
# "500 entries" was anywhere from ~15 MB to several hundred MB depending purely
# on which mix of images visitors happened to load — in a container with no
# memory limit set in docker-compose.yml. A byte budget makes the worst case a
# number we actually chose.
_POSTER_CACHE_MAX_BYTES = 64 * 1024 * 1024   # 64 MB of image data
_poster_cache = {}          # dicts are insertion-ordered, so this is FIFO
_poster_cache_bytes = 0
_poster_cache_lock = threading.Lock()


def _poster_cache_put(key, data: bytes, ct: str) -> None:
    """Insert one image, evicting oldest-first until the total is back under
    budget. Caller must NOT hold the lock; this takes it itself.

    A single image larger than the whole budget is simply not cached (rather
    than evicting everything to make room for it and still overflowing)."""
    global _poster_cache_bytes
    size = len(data)
    if size > _POSTER_CACHE_MAX_BYTES:
        return
    with _poster_cache_lock:
        existing = _poster_cache.pop(key, None)
        if existing is not None:
            _poster_cache_bytes -= len(existing[0])
        while _poster_cache and _poster_cache_bytes + size > _POSTER_CACHE_MAX_BYTES:
            oldest_key = next(iter(_poster_cache))       # dicts iterate in insertion order
            evicted_data, _ = _poster_cache.pop(oldest_key)
            _poster_cache_bytes -= len(evicted_data)
        _poster_cache[key] = (data, ct)
        _poster_cache_bytes += size


def _fetch_poster(id_, tag, h, kind="primary"):
    is_backdrop = kind == "backdrop"
    path = f"/Items/{urllib.parse.quote(id_)}/Images/{'Backdrop/0' if is_backdrop else 'Primary'}"
    params = {"maxHeight": h, "quality": 90, "api_key": JF_KEY}
    if tag:
        params["tag"] = tag
    url = f"{JF_URL}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read(), r.headers.get("Content-Type", "image/jpeg")


@router.get("/poster")
async def poster(id: str, tag: str = "", h: int = 360, kind: str = "primary"):
    """Server-side proxy for a Jellyfin image, so posters/backdrops load for
    anyone (no exposed key, no dependence on the visitor being logged into
    Jellyfin). kind=backdrop fetches the item's Backdrop/0 instead of its
    Primary poster — same token-server-side pattern either way."""
    if not (JF_KEY and id):
        return Response(status_code=404)
    kind = "backdrop" if kind == "backdrop" else "primary"
    h = max(90, min(h, 1600 if kind == "backdrop" else 720))
    cache_key = (id, tag, h, kind)

    with _poster_cache_lock:
        cached = _poster_cache.get(cache_key)
    if cached is not None:
        data, ct = cached
        return Response(content=data, media_type=ct,
                        headers={"Cache-Control": "public, max-age=86400"})

    try:
        data, ct = await asyncio.to_thread(_fetch_poster, id, tag, h, kind)
    except Exception:  # noqa: BLE001
        return Response(status_code=404)

    _poster_cache_put(cache_key, data, ct)
    return Response(content=data, media_type=ct,
                    headers={"Cache-Control": "public, max-age=86400"})
