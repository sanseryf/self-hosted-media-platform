"""Mock upstreams for the Hub demo.

One process impersonates every service Hub talks to, each on its usual port,
so Hub runs unmodified against them:

    8096  Jellyfin      (library, sessions, images, login)
    8989  Sonarr        (/api/v3/series, /queue)
    7878  Radarr        (/api/v3/movie, /queue, /diskspace)
    5055  Jellyseerr    (/api/v1/...)
    8088  Book requests (/api/search, /api/request)
    5000  Kavita        (health only)

All data comes from demo/catalog.py and is fictional. Posters and backdrops
are generated SVGs, so the demo needs no network access and ships no
copyrighted artwork.

Deliberate drift, so the reconciler has something real to find:
  * "The Velvet Archive" is in Radarr with a file on disk but not in Jellyfin
    (imported, never indexed).
  * "Northbound" is in Jellyfin but no library manager tracks it
    (added by hand).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import sys
import time
from html import escape
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from catalog import BY_ID, GENRES, ITEMS, PIPELINE, USERS  # noqa: E402

PORTS = {8096: "jellyfin", 8989: "sonarr", 7878: "radarr", 5055: "jellyseerr",
         8088: "books", 5000: "kavita"}
LIBRARIES = {"Movies": "lib-movies", "TV Shows": "lib-tv", "Anime": "lib-anime"}
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo")
STARTED = time.time()

app = FastAPI(title="Hub demo upstreams")


def _service(request: Request) -> str:
    port = request.scope.get("server", (None, None))[1]
    return PORTS.get(port, "jellyfin")


def _public(it: dict) -> dict:
    return {k: v for k, v in it.items() if not k.startswith("_") and k != "Library"}


# ── Generated artwork ─────────────────────────────────────────────────────────
def _poster_svg(it: dict, w: int = 400, h: int = 600) -> str:
    bg, mid, hi = it["_palette"]
    seed = int(hashlib.sha256(it["Id"].encode()).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    cx, cy, r = rnd.randint(90, 310), rnd.randint(150, 300), rnd.randint(70, 150)
    ridges = []
    for k in range(4):
        base = 380 + k * 45
        pts = " ".join(f"{x},{base + rnd.randint(-35, 25)}" for x in range(0, w + 50, 50))
        op = 0.35 + k * 0.17
        ridges.append(f'<polyline points="0,{h} {pts} {w},{h}" fill="{mid}" opacity="{op:.2f}"/>')
    title = escape(it["Name"])
    words, lines, cur = title.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) > 16 and cur:
            lines.append(cur)
            cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    lines.append(cur)
    ty = h - 40 - 34 * (len(lines) - 1)
    text = "".join(
        f'<text x="28" y="{ty + i * 34}" font-family="Georgia, serif" font-size="30" '
        f'font-weight="700" fill="{hi}">{ln}</text>' for i, ln in enumerate(lines))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{bg}"/><stop offset="1" stop-color="{mid}"/></linearGradient>
<radialGradient id="s"><stop offset="0" stop-color="{hi}" stop-opacity=".95"/>
<stop offset="1" stop-color="{hi}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="url(#g)"/>
<circle cx="{cx}" cy="{cy}" r="{r * 1.8:.0f}" fill="url(#s)" opacity=".35"/>
<circle cx="{cx}" cy="{cy}" r="{r}" fill="{hi}" opacity=".85"/>
{"".join(ridges)}
<rect y="{h - 150}" width="{w}" height="150" fill="{bg}" opacity=".55"/>
{text}
<text x="28" y="{h - 14}" font-family="Helvetica, Arial, sans-serif" font-size="13"
 letter-spacing="3" fill="{hi}" opacity=".75">{it["ProductionYear"]} · {escape(it["Genres"][0].upper())}</text>
</svg>'''


def _backdrop_svg(it: dict, w: int = 1280, h: int = 720) -> str:
    bg, mid, hi = it["_palette"]
    rnd = random.Random(it["Id"])
    layers = []
    for k in range(5):
        base = 380 + k * 70
        pts = " ".join(f"{x},{base + rnd.randint(-60, 40)}" for x in range(0, w + 80, 80))
        layers.append(f'<polyline points="0,{h} {pts} {w},{h}" fill="{mid}" opacity="{0.25 + k * 0.15:.2f}"/>')
    sx, sy = rnd.randint(700, 1100), rnd.randint(140, 260)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{bg}"/><stop offset=".7" stop-color="{mid}"/><stop offset="1" stop-color="{bg}"/></linearGradient>
<radialGradient id="s"><stop offset="0" stop-color="{hi}"/><stop offset="1" stop-color="{hi}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="url(#g)"/>
<circle cx="{sx}" cy="{sy}" r="260" fill="url(#s)" opacity=".45"/>
<circle cx="{sx}" cy="{sy}" r="90" fill="{hi}" opacity=".9"/>
{"".join(layers)}
</svg>'''


# ── Jellyfin ──────────────────────────────────────────────────────────────────
def _filter_items(q: dict) -> list[dict]:
    items = list(ITEMS)
    if q.get("Ids"):
        wanted = set(q["Ids"].split(","))
        return [it for it in items if it["Id"] in wanted]
    if q.get("AnyProviderIdEquals"):
        prov, _, val = q["AnyProviderIdEquals"].partition(".")
        key = {"tmdb": "Tmdb", "imdb": "Imdb", "tvdb": "Tvdb"}.get(prov.lower())
        items = [it for it in items if it["ProviderIds"].get(key) == val]
    if q.get("ParentId"):
        lib = next((n for n, i in LIBRARIES.items() if i == q["ParentId"]), None)
        items = [it for it in items if it["Library"] == lib]
    if q.get("IncludeItemTypes"):
        types = set(q["IncludeItemTypes"].split(","))
        items = [it for it in items if it["Type"] in types]
    if q.get("SearchTerm"):
        term = q["SearchTerm"].lower()
        items = [it for it in items if term in it["Name"].lower()]
    if q.get("Genres"):
        gs = {g.strip() for g in q["Genres"].replace("|", ",").split(",")}
        items = [it for it in items if gs & set(it["Genres"])]
    sort = (q.get("SortBy") or "SortName").split(",")[0]
    desc = (q.get("SortOrder") or "Ascending").startswith("Desc")
    if sort == "Random":
        random.shuffle(items)
    else:
        key = {"DateCreated": "DateCreated", "SortName": "SortName",
               "ProductionYear": "ProductionYear", "CommunityRating": "CommunityRating",
               "PremiereDate": "PremiereDate"}.get(sort, "SortName")
        items.sort(key=lambda it: it[key], reverse=desc)
    return items


def _sessions() -> list[dict]:
    """Three members mid-playback; positions advance in real time."""
    now = time.time()
    plays = [("maya", "demo0001", None, 0.18), ("jordan", "demo0006", (1, 4, "The Topping Out"), 0.55),
             ("riley", "demo0017", (2, 7, "Ashes of the Old Hearth"), 0.82)]
    out = []
    for i, (user, iid, ep, start_frac) in enumerate(plays):
        it = BY_ID[iid]
        total = it["RunTimeTicks"]
        pos = int((start_frac * total + (now - STARTED) * 10_000_000) % total)
        if ep:
            season, num, name = ep
            npi = {"Id": f"{iid}-s{season}e{num}", "Name": name, "Type": "Episode",
                   "SeriesName": it["Name"], "SeriesId": iid, "SeriesPrimaryImageTag": f"p{iid}",
                   "ParentIndexNumber": season, "IndexNumber": num,
                   "RunTimeTicks": total, "Genres": it["Genres"], "ProductionYear": it["ProductionYear"],
                   "ImageTags": {}}
        else:
            npi = {**_public(it)}
        out.append({"Id": f"session-{i}", "UserName": user, "Client": "Hub Web",
                    "DeviceName": ["Living room TV", "Laptop", "Phone"][i],
                    "NowPlayingItem": npi,
                    "PlayState": {"PositionTicks": pos, "IsPaused": False, "PlayMethod": "DirectPlay"}})
    out.append({"Id": "session-idle", "UserName": "sam", "Client": "Hub Web", "DeviceName": "Tablet"})
    return out


async def jellyfin(request: Request, path: str):
    q = dict(request.query_params)
    if path in ("health", ""):
        return PlainTextResponse("Healthy")
    if path == "System/Info":
        return {"ServerName": "demo", "Version": "10.11.0", "Id": "demo-server"}
    if path == "Items":
        items = _filter_items(q)
        start, limit = int(q.get("StartIndex", 0)), int(q.get("Limit", 1000))
        return {"Items": [_public(it) for it in items[start:start + limit]],
                "TotalRecordCount": len(items), "StartIndex": start}
    if path == "Genres":
        return {"Items": [{"Name": g, "Id": f"g-{g}"} for g in GENRES], "TotalRecordCount": len(GENRES)}
    if path == "Library/VirtualFolders":
        return [{"Name": n, "ItemId": i, "CollectionType": "movies" if n == "Movies" else "tvshows"}
                for n, i in LIBRARIES.items()]
    if path == "Sessions":
        return _sessions()
    if path.startswith("Items/") and "/Images/" in path:
        iid = path.split("/")[1].split("-s")[0]
        it = BY_ID.get(iid)
        if not it:
            return Response(status_code=404)
        svg = _backdrop_svg(it) if "/Backdrop" in path else _poster_svg(it)
        return Response(svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})
    if path == "Users/AuthenticateByName" and request.method == "POST":
        body = await request.json()
        user = (body.get("Username") or "").strip().lower()
        if user in USERS and body.get("Pw") == DEMO_PASSWORD:
            return {"User": {"Id": f"user-{user}", "Name": user},
                    "AccessToken": hashlib.sha256(f"demo-{user}".encode()).hexdigest()[:32]}
        return Response(status_code=401)
    return JSONResponse({"error": f"demo: {path} not implemented"}, status_code=404)


# ── Sonarr / Radarr ───────────────────────────────────────────────────────────
def _radarr_movies() -> list[dict]:
    out = [{"id": n, "title": it["Name"], "tmdbId": int(it["ProviderIds"]["Tmdb"]),
            "imdbId": it["ProviderIds"]["Imdb"], "hasFile": True, "monitored": True,
            "sizeOnDisk": 4_500_000_000 + n * 97_000_000, "status": "released"}
           for n, it in enumerate(ITEMS, 1)
           if it["Type"] == "Movie" and it["Name"] != "Northbound"]
    for k, (title, mtype, tmdb, _tvdb, state, _who) in enumerate(PIPELINE, 100):
        if mtype == "movie":
            out.append({"id": k, "title": title, "tmdbId": tmdb, "imdbId": None,
                        "hasFile": state in ("downloaded",), "monitored": True,
                        "sizeOnDisk": 6_200_000_000 if state == "downloaded" else 0,
                        "status": "released"})
    return out


def _sonarr_series() -> list[dict]:
    out = []
    for n, it in enumerate(ITEMS, 1):
        if it["Type"] != "Series":
            continue
        out.append({"id": n, "title": it["Name"], "tvdbId": int(it["ProviderIds"]["Tvdb"]),
                    "tmdbId": int(it["ProviderIds"]["Tmdb"]), "imdbId": it["ProviderIds"]["Imdb"],
                    "monitored": True, "status": "continuing",
                    "statistics": {"episodeFileCount": 10 + n, "episodeCount": 10 + n,
                                   "sizeOnDisk": 12_000_000_000 + n * 310_000_000}})
    for k, (title, mtype, tmdb, tvdb, _state, _who) in enumerate(PIPELINE, 100):
        if mtype == "series":
            out.append({"id": k, "title": title, "tvdbId": tvdb, "tmdbId": tmdb, "imdbId": None,
                        "monitored": True, "status": "upcoming",
                        "statistics": {"episodeFileCount": 0, "episodeCount": 8, "sizeOnDisk": 0}})
    return out


async def arr(request: Request, path: str, kind: str):
    q = dict(request.query_params)
    path = path.removeprefix("api/v3/")
    if path == "system/status":
        return {"appName": kind.title(), "version": "demo"}
    if path == "queue":
        return {"records": [], "totalRecords": 0}
    if path == "diskspace":
        return [{"path": "/data", "freeSpace": 3_100_000_000_000, "totalSpace": 8_000_000_000_000}]
    if kind == "radarr" and path == "movie":
        movies = _radarr_movies()
        if q.get("tmdbId"):
            movies = [m for m in movies if str(m["tmdbId"]) == q["tmdbId"]]
        return movies
    if kind == "sonarr" and path == "series":
        series = _sonarr_series()
        if q.get("tvdbId"):
            series = [s for s in series if str(s["tvdbId"]) == q["tvdbId"]]
        return series
    return JSONResponse({"error": f"demo: {path} not implemented"}, status_code=404)


# ── Jellyseerr ────────────────────────────────────────────────────────────────
def _seerr_result(it: dict) -> dict:
    mt = "movie" if it["Type"] == "Movie" else "tv"
    return {"id": int(it["ProviderIds"]["Tmdb"]), "mediaType": mt,
            ("title" if mt == "movie" else "name"): it["Name"],
            ("releaseDate" if mt == "movie" else "firstAirDate"): f"{it['ProductionYear']}-05-01",
            "overview": it["Overview"], "posterPath": None, "mediaInfo": {"status": 5}}


async def jellyseerr(request: Request, path: str):
    q = dict(request.query_params)
    path = path.removeprefix("api/v1/")
    if path == "status":
        return {"version": "demo", "commitTag": "demo"}
    if path.startswith("movie/") or path.startswith("tv/"):
        mt, _, tmdb = path.partition("/")
        hit = next((it for it in ITEMS if it["ProviderIds"]["Tmdb"] == tmdb), None)
        if hit:
            return {"title": hit["Name"], "name": hit["Name"],
                    "mediaInfo": {"id": int(tmdb) - 9_000_000, "status": 5}}
        pipe = next((p for p in PIPELINE if str(p[2]) == tmdb), None)
        if pipe:
            return {"title": pipe[0], "name": pipe[0],
                    "mediaInfo": {"id": int(tmdb) - 9_000_000, "status": 3, "tvdbId": pipe[3]}}
        return Response(status_code=404)
    if path == "request":
        if request.method == "POST":
            return {"id": random.randint(500, 900), "status": 1}
        pending = [{"id": 400 + i, "status": 1, "requestedBy": {"username": who}}
                   for i, (_t, _m, _tm, _tv, state, who) in enumerate(PIPELINE) if state == "requested"]
        return {"results": pending, "pageInfo": {"results": len(pending)}}
    if path == "user":
        return {"results": [{"id": i, "username": u, "jellyfinUsername": u, "displayName": u.title()}
                            for i, u in enumerate(USERS, 1)], "pageInfo": {"results": len(USERS)}}
    if path == "search":
        term = (q.get("query") or "").lower()
        return {"results": [_seerr_result(it) for it in ITEMS if term in it["Name"].lower()]}
    if path == "discover/trending":
        return {"results": [_seerr_result(it) for it in ITEMS[:10]]}
    return JSONResponse({"error": f"demo: {path} not implemented"}, status_code=404)


# ── Book-request service / Kavita ───────────────────────────────────────────────────────
_BOOKS = [("bk1", "book", "The Salt Road Ledger", "H. Ambrose"), ("bk2", "book", "Notes from a Quiet Engine", "D. Whitlock"),
          ("mg1", "manga", "Lantern Street", "A. Tanabe"), ("cm1", "comic", "Night Courier Vol. 1", "Y. Mori"),
          ("ab1", "audiobook", "The Salt Road Ledger (Audio)", "H. Ambrose")]


async def books(request: Request, path: str):
    q = dict(request.query_params)
    if path == "api/health":
        return {"ok": True, "book": {"ok": True}, "manga": {"ok": True}, "comic": {"ok": True}}
    if path == "api/search":
        term, kind = (q.get("q") or "").lower(), q.get("kind", "all")
        kinds = {"book", "manga", "comic"} if kind == "all" else {kind}
        return {"results": [{"id": i, "type": t, "title": ti, "author": a, "year": "2024"}
                            for i, t, ti, a in _BOOKS if t in kinds and term in ti.lower()]}
    if path == "api/request" and request.method == "POST":
        return {"ok": True, "message": "Requested (demo)"}
    return JSONResponse({"error": f"demo: {path} not implemented"}, status_code=404)


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "DELETE"])
async def dispatch(request: Request, path: str):
    svc = _service(request)
    if svc == "jellyfin":
        return await jellyfin(request, path)
    if svc in ("sonarr", "radarr"):
        return await arr(request, path, svc)
    if svc == "jellyseerr":
        return await jellyseerr(request, path)
    if svc == "books":
        return await books(request, path)
    return PlainTextResponse("Kavita (demo)")


async def main():
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, log_level="warning"))
               for p in PORTS]
    for s in servers:  # one app, many listeners — uvicorn installs signal handlers once
        s.install_signal_handlers = lambda: None
    print("demo upstreams listening on", ", ".join(f"{p} ({n})" for p, n in PORTS.items()), flush=True)
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
