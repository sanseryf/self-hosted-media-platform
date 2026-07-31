"""End-to-end lifecycle test — runs ONLY when DATABASE_URL points at a Postgres
(local staging or the box). No DB set → it prints SKIP and exits 0, so it's safe
in any CI/precommit run and becomes real verification the moment it has a DB.

It simulates L1 having ingested a realistic, deliberately OUT-OF-ORDER and
CROSS-KEYED event sequence for one title, drains it through L3, and asserts:
  • the four systems' events (Jellyseerr=tmdb, Sonarr=tvdb, Jellyfin=tmdb+tvdb)
    stitched into ONE lifecycle row (identity resolution across differing keys);
  • the final state is the furthest-along one despite out-of-order arrival;
  • the transition timeline recorded the real advances.

Run:  DATABASE_URL=postgres://... python hub/backend/tests/test_lifecycle_integration.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("SKIP: no DATABASE_URL set (this is the deploy/staging-time integration test).")
    sys.exit(0)

import asyncpg  # noqa: E402

import db  # noqa: E402
from projects import events, lifecycle  # noqa: E402

# Fake ids well outside any real range, so setup/teardown can target only our rows.
TMDB, TVDB = "999000001", "999000002"
MARK = "itest:"

# (source, event_type, ids...) — intentionally out of order: available before grab.
SEQ = [
    ("jellyseerr", "MEDIA_PENDING", {"tmdb_id": TMDB, "media_type": "movie", "title": "Test Title", "user_name": "itest"}),
    ("jellyfin",   "ItemAdded",     {"tmdb_id": TMDB, "tvdb_id": TVDB, "media_type": "movie", "title": "Test Title"}),
    ("sonarr",     "Grab",          {"tvdb_id": TVDB, "media_type": "movie", "title": "Test Title"}),  # late, lower rank
    ("jellyfin",   "PlaybackStart", {"tmdb_id": TMDB, "media_type": "movie", "title": "Test Title"}),
]


async def _cleanup(conn):
    await conn.execute("DELETE FROM lifecycle_transitions WHERE lifecycle_id IN "
                       "(SELECT id FROM lifecycle WHERE tmdb_id=$1 OR tvdb_id=$2)", TMDB, TVDB)
    await conn.execute("DELETE FROM lifecycle WHERE tmdb_id=$1 OR tvdb_id=$2", TMDB, TVDB)
    await conn.execute("DELETE FROM hub_events WHERE dedup_key LIKE $1", MARK + "%")


async def main():
    await db.connect()
    if db.pool() is None:
        print("FAIL: could not connect to DATABASE_URL")
        return 1
    await events.ensure_schema()
    await lifecycle.ensure_schema()

    results = []
    def check(name, cond):
        results.append(cond)
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")

    async with db.pool().acquire() as conn:
        await _cleanup(conn)
        # simulate L1 ingest: insert received events directly
        for i, (src, et, extra) in enumerate(SEQ):
            await conn.execute(
                """INSERT INTO hub_events (source,event_type,dedup_key,imdb_id,tmdb_id,tvdb_id,
                       title,media_type,user_name,payload)
                   VALUES ($1,$2,$3,NULL,$4,$5,$6,$7,$8,'{}'::jsonb)""",
                src, et, f"{MARK}{i}", extra.get("tmdb_id"), extra.get("tvdb_id"),
                extra.get("title"), extra.get("media_type"), extra.get("user_name"))

    res = await lifecycle.process_pending(500)
    check("drain ok", res.get("ok") is True)
    check("drained our 4 events (>=4 processed)", res.get("processed", 0) >= 4)

    async with db.pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM lifecycle WHERE tmdb_id=$1 OR tvdb_id=$2", TMDB, TVDB)
        check("4 cross-keyed events stitched into ONE title", len(rows) == 1)
        if rows:
            r = rows[0]
            check("final state = played (furthest, despite out-of-order)", r["state"] == "played")
            check("stitched both ids onto the row", r["tmdb_id"] == TMDB and r["tvdb_id"] == TVDB)
            check("kept the requester from the request event", r["requested_by"] == "itest")
            trans = await conn.fetch(
                "SELECT to_state FROM lifecycle_transitions WHERE lifecycle_id=$1 ORDER BY id", r["id"])
            states = [t["to_state"] for t in trans]
            check("timeline recorded advances (requested..available..played)",
                  "requested" in states and "available" in states and "played" in states)
            check("late 'grabbed' did NOT appear after available (no regress row)",
                  states == sorted(states, key=lambda s: lifecycle.L.RANK.get(s, 99)))
        await _cleanup(conn)

    await db.close()
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
