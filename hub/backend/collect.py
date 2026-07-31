"""Filesystem collector: snapshot library counts + sizes per media type into
Postgres. Handles real-world layout quirks: differing folder names, nested
title depth (manga series folders), case-duplicate dirs, and junk folders.
Run nightly (host cron) and once on deploy. Reads /data/media (mounted ro)."""
import asyncio
import os
from datetime import datetime, timezone

import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]
BASE = "/data/media"

# (label, [candidate folder names], title_depth)
#   title_depth = how many levels below the type folder a "title" sits.
#   Movies/TV/Anime/Comics: Type/<title>                     -> 1
#   Books:  books/<Author>/<Title>                           -> 2
#   Music:  Music/<Artist>/<Album>                           -> 2
#   Manga:  Manga/<library>/<group>/<title>                  -> 3
TYPES = [
    ("Movies", ["Movies"], 1),
    ("TV", ["TV Shows", "TV"], 1),
    ("Anime", ["Anime"], 1),
    ("Books", ["Books", "books"], 2),
    ("Comics", ["Comics"], 1),
    ("Manga", ["Manga"], 3),
]
EXCLUDE = {"thumbnails"}   # a reader app's cache dir; never a title

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_snapshots (
    id SERIAL PRIMARY KEY,
    taken_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    media_type TEXT NOT NULL,
    title_count INT NOT NULL,
    total_bytes BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS recent_items (
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL
);
"""


def _dirs_at_depth(base, depth):
    """Directories exactly `depth` levels under base (skipping hidden/excluded)."""
    out = []

    def walk(path, d):
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            if e.name.startswith(".") or e.name.lower() in EXCLUDE:
                continue
            if d == 1:
                try:
                    mt = e.stat().st_mtime
                except OSError:
                    mt = 0
                out.append((e.name, mt))
            else:
                walk(e.path, d - 1)

    walk(base, depth)
    return out


def _titles(base, depth):
    """Try the intended depth; fall back shallower if empty (handles flat libs)."""
    for d in range(depth, 0, -1):
        t = _dirs_at_depth(base, d)
        if t:
            return t
    return []


def _total_bytes(base):
    tot = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d.lower() not in EXCLUDE and not d.startswith(".")]
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(SCHEMA)
        all_recent = []
        for label, names, depth in TYPES:
            titles = []
            tbytes = 0
            for name in names:
                base = os.path.join(BASE, name)
                if os.path.isdir(base):
                    titles += _titles(base, depth)
                    tbytes += _total_bytes(base)
            await conn.execute(
                "INSERT INTO library_snapshots(media_type,title_count,total_bytes) VALUES($1,$2,$3)",
                label, len(titles), tbytes,
            )
            for nm, mt in titles:
                if mt:
                    all_recent.append((label, nm, datetime.fromtimestamp(mt, timezone.utc)))
            print(f"{label}: {len(titles)} titles, {tbytes/1e9:.1f} GB")
        all_recent.sort(key=lambda r: r[2], reverse=True)
        await conn.execute("TRUNCATE recent_items")
        for label, name, dt in all_recent[:12]:
            await conn.execute(
                "INSERT INTO recent_items(media_type,title,added_at) VALUES($1,$2,$3)",
                label, name, dt,
            )
        print("snapshot complete")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
