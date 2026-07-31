"""Watch poller for Media Hub.

Polls Jellyfin /Sessions and drops one heartbeat per actively-playing
session into `watch_heartbeats`, caching item + genre metadata. Over time,
SUM(minutes) per genre approximates real watch time (see fn_watch_by_genre).

Run every POLL_MINUTES via host cron:
    */5 * * * * docker exec hub python /app/backend/watch_poll.py

Env:
    DATABASE_URL      (required)
    JELLYFIN_URL      default http://10.0.0.10:8096
    JELLYFIN_API_KEY  (required for real data; script no-ops without it)
    POLL_MINUTES      default 5  (must match the cron cadence)
"""
import asyncio
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]
JF_URL = os.environ.get("JELLYFIN_URL", "http://10.0.0.10:8096").rstrip("/")
JF_KEY = os.environ.get("JELLYFIN_API_KEY", "")
POLL_MIN = float(os.environ.get("POLL_MINUTES", "5"))
SQL_FILE = Path(__file__).parent / "sql" / "watching_schema.sql"


def jf_get(path, **params):
    params["api_key"] = JF_KEY
    url = f"{JF_URL}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def genres_for(item):
    """Genres off the now-playing item; fall back to the series for episodes."""
    g = item.get("Genres") or []
    if g:
        return g
    try:
        full = jf_get(f"/Items/{item['Id']}", fields="Genres")
        g = full.get("Genres") or []
        if not g and item.get("SeriesId"):
            series = jf_get(f"/Items/{item['SeriesId']}", fields="Genres")
            g = series.get("Genres") or []
    except Exception:  # noqa: BLE001
        g = []
    return g


async def main():
    if not JF_KEY:
        print("JELLYFIN_API_KEY not set — nothing to poll.")
        return

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(SQL_FILE.read_text())

        try:
            sessions = jf_get("/Sessions")
        except Exception as ex:  # noqa: BLE001
            print(f"Could not reach Jellyfin: {ex}")
            return

        beats = 0
        for s in sessions:
            item = s.get("NowPlayingItem")
            state = s.get("PlayState") or {}
            if not item or state.get("IsPaused"):
                continue
            if item.get("Type") not in ("Movie", "Episode"):
                continue

            item_id = item["Id"]
            runtime = item.get("RunTimeTicks")
            runtime_min = round(runtime / 600_000_000, 2) if runtime else None
            await conn.execute(
                """INSERT INTO jf_items(item_id,name,media_type,series_name,runtime_min,updated_at)
                   VALUES($1,$2,$3,$4,$5,now())
                   ON CONFLICT (item_id) DO UPDATE
                     SET name=$2, media_type=$3, series_name=$4,
                         runtime_min=COALESCE($5,jf_items.runtime_min), updated_at=now()""",
                item_id, item.get("Name"), item.get("Type"),
                item.get("SeriesName"), runtime_min,
            )
            for genre in genres_for(item):
                await conn.execute(
                    "INSERT INTO jf_item_genres(item_id,genre) VALUES($1,$2) "
                    "ON CONFLICT DO NOTHING",
                    item_id, genre,
                )

            user = s.get("UserName")
            session_id = f"{s.get('Id','?')}::{item_id}"
            await conn.execute(
                "INSERT INTO watch_heartbeats(session_id,item_id,user_name,minutes) "
                "VALUES($1,$2,$3,$4)",
                session_id, item_id, user, POLL_MIN,
            )
            beats += 1
            print(f"  ♪ {user or '?'} — {item.get('SeriesName') or ''} "
                  f"{item.get('Name')}".rstrip())

        print(f"Recorded {beats} watch heartbeat(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
