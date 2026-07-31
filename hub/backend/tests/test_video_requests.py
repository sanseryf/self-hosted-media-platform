"""Tests for the in-site video request proxy (discover.py) — search shaping,
per-user attribution, and submit payload construction. Jellyseerr is mocked, so
no live calls and no real requests are made.

Run:  python hub/backend/tests/test_video_requests.py
"""
import asyncio
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projects import discover  # noqa: E402

discover.JS_KEY = "testkey"  # enable the endpoints (normally from env)

USERS = {"results": [
    {"id": 7, "jellyfinUsername": "alice", "username": "alice"},
    {"id": 9, "username": "bob"},
]}
SEARCH = {"results": [
    {"id": 438631, "mediaType": "movie", "title": "Starfall Protocol", "releaseDate": "2021-10-22",
     "posterPath": "/a.jpg", "overview": "x", "mediaInfo": {"status": 1}},
    {"id": 1, "mediaType": "tv", "name": "The Long Meridian", "firstAirDate": "2022-09-21",
     "posterPath": "/b.jpg", "overview": "y", "mediaInfo": {"status": 5}},
    {"id": 99, "mediaType": "person", "name": "Someone"},          # not requestable → dropped
    {"id": 5, "mediaType": "movie", "title": "NoArt", "posterPath": None},  # no poster → dropped
]}


def fake_get(path, **params):
    if path == "/user":
        # single page
        return USERS if params.get("skip", 0) == 0 else {"results": []}
    if path == "/search":
        return SEARCH
    return {"results": []}


results = []
def check(name, cond):
    results.append(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


async def main():
    discover._js_get = fake_get
    discover._user_map, discover._user_map_ts = {}, 0.0  # reset cache

    # search shaping
    r = await discover.search("starfall")
    titles = [i["title"] for i in r["items"]]
    check("search keeps movie+tv, drops person & poster-less", titles == ["Starfall Protocol", "The Long Meridian"])
    check("search tags availability", r["items"][1]["status"] == "available")
    check("search item type + tmdb id", r["items"][0]["type"] == "movie" and r["items"][0]["id"] == 438631)

    # attribution resolution
    check("resolve alice -> 7", discover._jellyseerr_user_id("alice") == 7)
    check("resolve bob -> 9 (by username)", discover._jellyseerr_user_id("bob") == 9)
    check("unknown user -> None", discover._jellyseerr_user_id("carol") is None)

    # submit payload construction (capture what would be POSTed)
    captured = {}
    def fake_post(path, body):
        captured["path"], captured["body"] = path, body
        return {"id": 555}
    discover._js_post = fake_post

    async def ident_alice(_): return ("sid", "alice")
    async def ident_carol(_): return ("sid", "carol")

    discover.auth.get_session_identity = ident_alice
    out = await discover.submit(discover._SubmitBody(tmdb_id=438631, media_type="movie"), object())
    check("movie submit payload", captured["body"] == {"mediaType": "movie", "mediaId": 438631, "userId": 7})
    check("movie submit attributed", out["ok"] and out["attributed_to"] == "alice")

    out = await discover.submit(discover._SubmitBody(tmdb_id=1, media_type="tv"), object())
    check("tv submit requests all seasons + attributed",
          captured["body"] == {"mediaType": "tv", "mediaId": 1, "seasons": "all", "userId": 7})

    discover.auth.get_session_identity = ident_carol
    out = await discover.submit(discover._SubmitBody(tmdb_id=2, media_type="movie"), object())
    check("unmapped user submits unattributed (no userId)",
          "userId" not in captured["body"] and out["attributed_to"] is None)

    # 409 already-requested handling
    def post_409(path, body):
        raise urllib.error.HTTPError("http://x", 409, "Conflict", {}, None)
    discover._js_post = post_409
    out = await discover.submit(discover._SubmitBody(tmdb_id=3, media_type="movie"), object())
    check("409 -> already requested (ok, not error)", out.get("ok") and out.get("already"))

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
