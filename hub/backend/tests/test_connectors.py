"""Unit tests for the L2 connectors — driven by httpx.MockTransport, so they
exercise real request building + the whole resilience/error taxonomy with zero
live servers and zero DB.

Run:  python hub/backend/tests/test_connectors.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import connectors as C  # noqa: E402
from connectors.base import (  # noqa: E402
    ConnectorAuthError, ConnectorBadResponse, ConnectorNotFound,
)
from connectors.jellyfin import JellyfinConnector  # noqa: E402
from connectors.jellyseerr import JellyseerrConnector  # noqa: E402
from connectors.radarr import RadarrConnector  # noqa: E402
from connectors.sonarr import SonarrConnector  # noqa: E402


def _json(payload, status=200):
    return httpx.Response(status, json=payload)


def _mk(cls, handler, *, key="secret", url="http://up"):
    """A connector wired to a MockTransport; backoff_base=0 keeps retries instant."""
    return cls(url, key, backoff_base=0, transport=httpx.MockTransport(handler))


results = []
def check(name, cond):
    results.append(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


# ── request building + auth headers + parsing ─────────────────────────────────
async def t_radarr_parse_and_auth():
    seen = {}
    def h(req):
        seen["path"] = req.url.path
        seen["tmdbId"] = req.url.params.get("tmdbId")
        seen["auth"] = req.headers.get("X-Api-Key")
        return _json([{"id": 12, "title": "Starfall Protocol", "tmdbId": 438631,
                       "imdbId": "tt1160419", "hasFile": True, "monitored": True}])
    r = _mk(RadarrConnector, h)
    ref = await r.movie_by_tmdb(438631)
    await r.aclose()
    check("radarr path /api/v3/movie", seen["path"] == "/api/v3/movie")
    check("radarr sends X-Api-Key header", seen["auth"] == "secret")
    check("radarr coerces tmdb id to str", ref.tmdb_id == "438631")
    check("radarr hasFile -> downloaded", ref.state == C.SystemState.downloaded)


async def t_radarr_absent_and_monitored():
    r_absent = _mk(RadarrConnector, lambda req: _json([]))
    ref = await r_absent.movie_by_tmdb(1)
    await r_absent.aclose()
    check("radarr empty list -> None (absent, not error)", ref is None)

    r_mon = _mk(RadarrConnector, lambda req: _json(
        [{"id": 1, "title": "X", "tmdbId": 5, "hasFile": False, "monitored": True}]))
    ref = await r_mon.movie_by_tmdb(5)
    await r_mon.aclose()
    check("radarr monitored-no-file -> monitored", ref.state == C.SystemState.monitored)


async def t_sonarr_series():
    def h(req):
        return _json([{"id": 3, "title": "The Long Meridian", "tvdbId": 368211, "monitored": True,
                       "statistics": {"episodeFileCount": 12, "episodeCount": 12}}])
    s = _mk(SonarrConnector, h)
    ref = await s.series_by_tvdb(368211)
    await s.aclose()
    check("sonarr keys on tvdb", ref.tvdb_id == "368211")
    check("sonarr with files -> downloaded", ref.state == C.SystemState.downloaded)


async def t_jellyfin_provider_query():
    seen = {}
    def h(req):
        seen["needle"] = req.url.params.get("AnyProviderIdEquals")
        seen["auth"] = req.headers.get("X-Emby-Token")
        return _json({"Items": [{"Id": "jf1", "Name": "Starfall Protocol", "Type": "Movie",
                                 "ProviderIds": {"Tmdb": "438631", "Imdb": "tt1160419"}}]})
    j = _mk(JellyfinConnector, h)
    ref = await j.find_by_provider(tmdb="438631")
    await j.aclose()
    check("jellyfin AnyProviderIdEquals lowercased 'tmdb.438631'", seen["needle"] == "tmdb.438631")
    check("jellyfin sends X-Emby-Token", seen["auth"] == "secret")
    check("jellyfin present -> available", ref.state == C.SystemState.available)


async def t_jellyseerr_status_and_routing():
    seen = {}
    def h(req):
        seen["path"] = req.url.path
        return _json({"title": "Starfall Protocol", "mediaInfo": {"id": 9, "status": 5}})
    js = _mk(JellyseerrConnector, h)
    ref = await js.media_by_tmdb(438631, "movie")
    await js.aclose()
    check("jellyseerr movie routes to /api/v1/movie/{id}", seen["path"] == "/api/v1/movie/438631")
    check("jellyseerr status 5 -> available", ref.state == C.SystemState.available)

    js_tv = _mk(JellyseerrConnector, h)
    await js_tv.media_by_tmdb(999, "tv")
    await js_tv.aclose()
    check("jellyseerr tv routes to /api/v1/tv/{id}", seen["path"] == "/api/v1/tv/999")

    # no media_type -> can't choose endpoint -> None, and makes NO request
    called = {"n": 0}
    def h2(req):
        called["n"] += 1
        return _json({})
    js_none = _mk(JellyseerrConnector, h2)
    ref = await js_none.media_by_tmdb(1, None)
    await js_none.aclose()
    check("jellyseerr no media_type -> None, no request", ref is None and called["n"] == 0)

    js_absent = _mk(JellyseerrConnector, lambda req: _json({"title": "Y", "mediaInfo": None}))
    ref = await js_absent.media_by_tmdb(2, "movie")
    await js_absent.aclose()
    check("jellyseerr mediaInfo null -> absent", ref.state == C.SystemState.absent)


# ── resilience / error taxonomy ───────────────────────────────────────────────
async def t_retry_on_5xx_then_success():
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream busy")
        return _json([])
    r = _mk(RadarrConnector, h)
    ref = await r.movie_by_tmdb(1)
    await r.aclose()
    check("retries transient 5xx then succeeds (3 attempts)", calls["n"] == 3 and ref is None)


async def t_no_retry_on_auth():
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        return httpx.Response(401, text="nope")
    r = _mk(RadarrConnector, h)
    raised = False
    try:
        await r.movie_by_tmdb(1)
    except ConnectorAuthError:
        raised = True
    await r.aclose()
    check("401 raises ConnectorAuthError with NO retry", raised and calls["n"] == 1)


async def t_404_becomes_none():
    j = _mk(JellyfinConnector, lambda req: httpx.Response(404))
    ref = await j.find_by_provider(tmdb="1")
    await j.aclose()
    check("jellyfin 404 -> None", ref is None)

    js = _mk(JellyseerrConnector, lambda req: httpx.Response(404))
    ref = await js.media_by_tmdb(1, "movie")
    await js.aclose()
    check("jellyseerr 404 -> None", ref is None)


async def t_bad_response_raises():
    r = _mk(RadarrConnector, lambda req: httpx.Response(200, text="<html>not json</html>"))
    raised = False
    try:
        await r.movie_by_tmdb(1)
    except ConnectorBadResponse:
        raised = True
    await r.aclose()
    check("non-JSON 200 -> ConnectorBadResponse", raised)


async def t_disabled_makes_no_calls():
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        return _json([])
    # empty key -> disabled
    r = RadarrConnector("http://up", "", backoff_base=0, transport=httpx.MockTransport(h))
    ref = await r.movie_by_tmdb(1)
    hs = await r.health()
    await r.aclose()
    check("disabled connector: read -> None, no network call", ref is None and calls["n"] == 0)
    check("disabled connector: health ok=False 'not configured'",
          (not hs.ok) and hs.detail == "not configured")


async def t_gateway_locate_isolates_failures():
    # jellyfin errors, radarr ok — locate() must return radarr's view and None for jellyfin
    jf = JellyfinConnector("http://up", "k", backoff_base=0,
                           transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    ra = _mk(RadarrConnector, lambda req: _json(
        [{"id": 1, "title": "Starfall Protocol", "tmdbId": 438631, "hasFile": True, "monitored": True}]))
    so = RadarrConnector("", "")  # disabled stand-in (series path skipped for movie anyway)
    js = _mk(JellyseerrConnector, lambda req: _json({"title": "Starfall Protocol", "mediaInfo": {"status": 5}}))
    gw = C.Gateway(jf, ra, so, js)
    views = await gw.locate(tmdb="438631", media_type="movie")
    await gw.aclose()
    check("locate: failing jellyfin -> None, healthy radarr -> view",
          views["jellyfin"] is None and views["radarr"].state == C.SystemState.downloaded)
    check("locate: movie skips sonarr branch", views["sonarr"] is None)


async def main():
    for t in [t_radarr_parse_and_auth, t_radarr_absent_and_monitored, t_sonarr_series,
              t_jellyfin_provider_query, t_jellyseerr_status_and_routing,
              t_retry_on_5xx_then_success, t_no_retry_on_auth, t_404_becomes_none,
              t_bad_response_raises, t_disabled_makes_no_calls,
              t_gateway_locate_isolates_failures]:
        await t()
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
