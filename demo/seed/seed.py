"""Seed the Hub demo with history so every screen has something to show.

Runs once after Hub is healthy. It does two kinds of work:

  1. Writes history tables directly — library growth, watch time, service
     uptime — because in production those accumulate over months from cron
     jobs (collect.py, watch_poll.py, the status sampler).
  2. Drives the event pipeline through Hub's real public interface: it POSTs
     Sonarr/Radarr/Jellyseerr/Jellyfin webhook payloads to
     /api/events/ingest, then asks Hub to drain the inbox, run the reconciler
     and evaluate the approval policy. Nothing in Hub is stubbed; the
     lifecycle rows you see are the state machine's own output.

Finally it spreads the event timestamps over the past few days, so the
journey timeline reads like real history and one title is visibly stalled.
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from catalog import ITEMS, NOW, PIPELINE  # noqa: E402

HUB = os.environ.get("HUB_URL", "http://hub:8080").rstrip("/")
DB = os.environ["DATABASE_URL"]
TOKEN = os.environ.get("EVENTS_TOKEN", "demo-token")
USER, PASSWORD = os.environ.get("DEMO_USER", "demo"), os.environ.get("DEMO_PASSWORD", "demo")
SQL_DIR = Path(os.environ.get("HUB_SQL_DIR", "/app/backend/sql"))
rnd = random.Random(42)


async def wait_for_hub(client: httpx.AsyncClient) -> None:
    for _ in range(90):
        try:
            r = await client.get(f"{HUB}/api/health")
            if r.status_code == 200 and r.json().get("db", {}).get("ok"):
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2)
    raise SystemExit("Hub never became healthy")


# ── 1. history tables ─────────────────────────────────────────────────────────
LIBRARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_snapshots (
    id SERIAL PRIMARY KEY, taken_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    media_type TEXT NOT NULL, title_count INT NOT NULL, total_bytes BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS recent_items (
    media_type TEXT NOT NULL, title TEXT NOT NULL, added_at TIMESTAMPTZ NOT NULL);
"""
# (type, titles at day 0, titles today, avg GB per title)
GROWTH = [("Movies", 140, 236, 6.5), ("TV", 38, 61, 42.0), ("Anime", 12, 27, 18.0),
          ("Books", 210, 388, 0.004), ("Comics", 22, 47, 0.09), ("Manga", 30, 74, 0.25)]


async def seed_history(conn: asyncpg.Connection) -> None:
    await conn.execute(LIBRARY_SCHEMA)
    await conn.execute((SQL_DIR / "watching_schema.sql").read_text())
    await conn.execute("TRUNCATE library_snapshots, recent_items, watch_heartbeats, jf_items, jf_item_genres")

    days = 120
    rows = []
    for d in range(days, -1, -1):
        t = NOW - timedelta(days=d, hours=-3)
        frac = (days - d) / days
        for mtype, start, end, gb in GROWTH:
            # an S-curve with weekly noise reads more like a real library than a line
            count = int(start + (end - start) * (0.5 - 0.5 * math.cos(math.pi * frac)) + rnd.randint(-1, 1))
            rows.append((t, mtype, max(count, start), int(count * gb * 1e9)))
    await conn.executemany(
        "INSERT INTO library_snapshots(taken_at, media_type, title_count, total_bytes) VALUES($1,$2,$3,$4)", rows)

    recent = sorted(ITEMS, key=lambda it: it["DateCreated"], reverse=True)[:12]
    await conn.executemany(
        "INSERT INTO recent_items(media_type, title, added_at) VALUES($1,$2,$3)",
        [("Movies" if it["Type"] == "Movie" else "TV", it["Name"],
          datetime.fromisoformat(it["DateCreated"])) for it in recent])

    await conn.executemany(
        "INSERT INTO jf_items(item_id, name, media_type, series_name, runtime_min) VALUES($1,$2,$3,$4,$5)",
        [(it["Id"], it["Name"], "Movie" if it["Type"] == "Movie" else "Episode",
          None if it["Type"] == "Movie" else it["Name"], it["RunTimeTicks"] / 600_000_000) for it in ITEMS])
    await conn.executemany("INSERT INTO jf_item_genres(item_id, genre) VALUES($1,$2)",
                           [(it["Id"], g) for it in ITEMS for g in it["Genres"]])
    beats = []
    users = ["maya", "jordan", "sam", "riley", "alex"]
    for n in range(260):
        it = rnd.choice(ITEMS)
        start = NOW - timedelta(days=rnd.uniform(0, 60))
        minutes = int(it["RunTimeTicks"] / 600_000_000 * rnd.uniform(0.4, 1.0))
        for k in range(0, minutes, 5):
            beats.append((start + timedelta(minutes=k), f"s{n}-{it['Id']}", it["Id"], rnd.choice(users), 5))
    await conn.executemany(
        "INSERT INTO watch_heartbeats(seen_at, session_id, item_id, user_name, minutes) VALUES($1,$2,$3,$4,$5)",
        beats)

    # Uptime board: every 15 minutes for 30 days, with two realistic incidents.
    await conn.execute("DELETE FROM service_history; DELETE FROM status_history;")
    keys = ["watch", "request", "read", "browse"]
    svc, overall = [], []
    for step in range(30 * 96, 0, -1):
        t = NOW - timedelta(minutes=15 * step)
        days_ago = step / 96
        outage_all = 11.0 < days_ago < 11.06      # network loss: everything down ~1.5h
        outage_watch = 17.0 < days_ago < 17.6     # media server down after a kernel update
        ups = []
        for k in keys:
            up = not (outage_all or (outage_watch and k == "watch"))
            svc.append((t, k, up))
            ups.append(up)
        n = sum(ups)
        overall.append((t, n, len(keys), "live" if n == len(keys) else ("down" if n == 0 else "partial")))
    await conn.executemany("INSERT INTO service_history(checked_at, service_key, up) VALUES($1,$2,$3)", svc)
    await conn.executemany(
        "INSERT INTO status_history(checked_at, up, total, overall) VALUES($1,$2,$3,$4)", overall)
    print(f"history: {len(rows)} snapshots, {len(beats)} watch beats, {len(svc)} uptime samples")


# ── 2. the event pipeline, through Hub's real API ─────────────────────────────
def _arr_payload(kind, event, title, tmdb, tvdb=None):
    if kind == "radarr":
        return {"eventType": event, "movie": {"title": title, "tmdbId": tmdb, "imdbId": None},
                "downloadId": f"dl-{tmdb}" if event != "MovieAdded" else None,
                "movieFile": {"relativePath": f"{title}.mkv"} if event == "Download" else {}}
    return {"eventType": event, "series": {"title": title, "tmdbId": tmdb, "tvdbId": tvdb},
            "episodes": [{"id": tvdb * 10 + 1}], "downloadId": f"dl-{tvdb}"}


def _seerr_payload(event, title, tmdb, tvdb, mtype, who, req_id):
    return {"notification_type": event, "subject": title,
            "media": {"tmdbId": str(tmdb), "tvdbId": str(tvdb) if tvdb else None, "media_type": mtype},
            "request": {"request_id": str(req_id), "requestedBy_username": who}}


def _jf_payload(event, title, tmdb, tvdb, item_type, user=None, ts=None):
    return {"NotificationType": event, "Name": title, "ItemType": item_type,
            "Provider_Tmdb": str(tmdb), **({"Provider_Tvdb": str(tvdb)} if tvdb else {}),
            "ItemId": f"jf-{tmdb}", "NotificationUsername": user,
            "UtcTimestamp": ts or NOW.isoformat()}


def build_events():
    """Return [(source, payload)] in a deliberately imperfect order."""
    ev = []
    ladder_full = ["requested", "grabbed", "downloaded", "available", "played"]
    who = ["demo", "jordan", "demo", "riley", "demo", "maya"]
    # Titles that made it all the way through (the most recently added library items).
    done = [it for it in sorted(ITEMS, key=lambda i: i["DateCreated"], reverse=True)[:6]]
    for n, it in enumerate(done):
        tmdb = int(it["ProviderIds"]["Tmdb"])
        tvdb = int(it["ProviderIds"]["Tvdb"]) if "Tvdb" in it["ProviderIds"] else None
        movie = it["Type"] == "Movie"
        kind = "radarr" if movie else "sonarr"
        mtype = "movie" if movie else "tv"
        user = who[n % len(who)]
        stop = ladder_full if n < 4 else ladder_full[:-1]       # two not watched yet
        if "requested" in stop:
            ev.append(("jellyseerr", _seerr_payload("MEDIA_APPROVED", it["Name"], tmdb, tvdb, mtype, user, 300 + n)))
        if "grabbed" in stop:
            ev.append((kind, _arr_payload(kind, "Grab", it["Name"], tmdb, tvdb)))
        if "downloaded" in stop:
            ev.append((kind, _arr_payload(kind, "Download", it["Name"], tmdb, tvdb)))
        if "available" in stop:
            ev.append(("jellyfin", _jf_payload("ItemAdded", it["Name"], tmdb, tvdb, it["Type"])))
        if "played" in stop:
            ev.append(("jellyfin", _jf_payload("PlaybackStart", it["Name"], tmdb, tvdb, it["Type"], user)))
    # Out-of-order delivery: a retried Grab for an already-available title.
    first = done[0]
    ev.append(("radarr" if first["Type"] == "Movie" else "sonarr",
               _arr_payload("radarr", "Grab", first["Name"], int(first["ProviderIds"]["Tmdb"]))))
    # The in-flight pipeline.
    for n, (title, mtype, tmdb, tvdb, state, user) in enumerate(PIPELINE):
        kind = "radarr" if mtype == "movie" else "sonarr"
        # Still-pending requests go through the shadow-mode approval policy.
        event = "MEDIA_PENDING" if state == "requested" else "MEDIA_APPROVED"
        ev.append(("jellyseerr", _seerr_payload(event, title, tmdb, tvdb,
                                                "movie" if mtype == "movie" else "tv", user, 400 + n)))
        if state in ("grabbed", "downloading", "downloaded"):
            ev.append((kind, _arr_payload(kind, "Grab", title, tmdb, tvdb)))
        if state == "downloaded":
            ev.append((kind, _arr_payload(kind, "Download", title, tmdb, tvdb)))
    # A malformed payload, to show the dead-letter path.
    ev.append(("sonarr", {"note": "not a webhook payload"}))
    return ev


async def drive_pipeline(client: httpx.AsyncClient) -> None:
    r = await client.post(f"{HUB}/api/auth/login", json={"username": USER, "password": PASSWORD})
    r.raise_for_status()
    events = build_events()
    for source, payload in events:
        await client.post(f"{HUB}/api/events/ingest/{source}", json=payload,
                          headers={"X-Hub-Token": TOKEN})
    for path in ("/api/lifecycle/drain", "/api/reconcile/run", "/api/policy/evaluate-pending"):
        r = await client.post(f"{HUB}{path}")
        print(f"{path}: HTTP {r.status_code}")
    print(f"pipeline: {len(events)} webhooks ingested")


# ── 3. make the timeline read like history ────────────────────────────────────
async def spread_timestamps(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch("SELECT id, title, state FROM lifecycle ORDER BY id")
    stalled = {"Harbor of Glass"}   # parked past the 24h stall threshold
    for n, row in enumerate(rows):
        trans = await conn.fetch(
            "SELECT id FROM lifecycle_transitions WHERE lifecycle_id=$1 ORDER BY at, id", row["id"])
        if row["title"] in stalled:
            end = NOW - timedelta(hours=30 + n)            # past the 24h stall threshold
        else:
            end = NOW - timedelta(hours=2 + n * 7)
        gaps = [timedelta(hours=rnd.uniform(0.4, 9)) for _ in trans]
        t = end - sum(gaps, timedelta())
        stamps = []
        for tr, gap in zip(trans, gaps):
            t += gap
            stamps.append(t)
            await conn.execute("UPDATE lifecycle_transitions SET at=$2 WHERE id=$1", tr["id"], t)
        if stamps:
            await conn.execute(
                "UPDATE lifecycle SET first_seen_at=$2, state_since=$3, updated_at=$3 WHERE id=$1",
                row["id"], stamps[0], stamps[-1])
    await conn.execute(
        "UPDATE hub_events SET received_at = now() - (random() * interval '4 days') "
        "WHERE received_at > now() - interval '1 hour'")
    print(f"timeline: spread {len(rows)} lifecycle rows")


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await wait_for_hub(client)
        conn = await asyncpg.connect(DB)
        try:
            await seed_history(conn)
            await drive_pipeline(client)
            await spread_timestamps(conn)
        finally:
            await conn.close()
    print(f"demo ready — sign in as '{USER}' / '{PASSWORD}'")


if __name__ == "__main__":
    asyncio.run(main())
