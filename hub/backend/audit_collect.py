"""File-audit collector for Media Hub.

Walks /data/media, records one row per directory into `fs_entries`, then:
  * AUTO-PURGES truly-empty folders (zero files anywhere below), bottom-up.
  * REPORTS duplicate titles (never deletes them).
Logs each run into `audit_runs`.

Run on deploy and nightly:
    docker exec hub python /app/backend/audit_collect.py
Cron (host):
    30 3 * * * docker exec hub python /app/backend/audit_collect.py

NOTE: the mount is read-only for the stats collector, but this maintenance
job needs write access to purge. Mount /srv/media/media read-WRITE for the
hub (docker-compose) OR run this script on the host with DATABASE_URL set and
BASE pointed at the real path. It refuses to delete top-level type folders.
"""
import asyncio
import os
import re
import shutil
from pathlib import Path

import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]
BASE = os.environ.get("MEDIA_BASE", "/data/media")
SQL_FILE = Path(__file__).parent / "sql" / "audit_functions.sql"
PURGE = os.environ.get("AUDIT_PURGE_EMPTIES", "1") == "1"  # auto-delete empties

# (label, [candidate folder names], title_depth) — mirrors collect.py.
# title_depth = levels below the type folder where a "title" sits.
TYPES = [
    ("Movies", ["Movies"], 1),
    ("TV", ["TV Shows", "TV"], 1),
    ("Anime", ["Anime"], 1),
    ("Books", ["Books", "books"], 2),
    ("Comics", ["Comics"], 1),
    ("Manga", ["Manga"], 3),
]
EXCLUDE = {"thumbnails"}                       # a reader app's cache dir; never a title
FOLDER_TO_TYPE = {n.lower(): lbl for lbl, names, _ in TYPES for n in names}
TITLE_DEPTH = {lbl: d for lbl, _, d in TYPES}  # by label

# strip year/edition/quality tags so "Inception (2010)" == "inception"
_TAG = re.compile(r"[\(\[\{].*?[\)\]\}]|\b(480p|576p|720p|1080p|2160p|4k|hdr|remux)\b", re.I)


def norm(name: str) -> str:
    s = _TAG.sub(" ", name)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s or name.lower()


class Node:
    __slots__ = ("path", "name", "depth", "child_dirs", "file_count",
                 "subtree_files", "subtree_bytes")

    def __init__(self, path, name, depth):
        self.path = path
        self.name = name
        self.depth = depth
        self.child_dirs = 0
        self.file_count = 0
        self.subtree_files = 0
        self.subtree_bytes = 0


def scan(base):
    """Recursively collect Node objects with rolled-up subtree stats.
    depth is relative to `base`: a top-level type folder is depth 1."""
    entries = []

    def walk(abspath, relpath, depth):
        node = Node(relpath, os.path.basename(relpath), depth)
        try:
            items = list(os.scandir(abspath))
        except OSError:
            entries.append(node)
            return node
        for e in items:
            if e.name.startswith("."):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if e.name.lower() in EXCLUDE:
                    # junk dir: count its bytes but don't treat as a title dir
                    node.subtree_bytes += _dir_bytes(e.path)
                    continue
                node.child_dirs += 1
                child = walk(e.path, os.path.join(relpath, e.name), depth + 1)
                node.subtree_files += child.subtree_files
                node.subtree_bytes += child.subtree_bytes
            else:
                try:
                    node.subtree_bytes += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                node.file_count += 1
                node.subtree_files += 1
        entries.append(node)
        return node

    for e in sorted(os.scandir(base), key=lambda x: x.name):
        if not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
            continue
        walk(e.path, e.name, 1)
    return entries


def _dir_bytes(path):
    tot = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot


def classify(relpath, depth):
    """Return (media_type, is_title) for a dir at `relpath`/`depth`."""
    top = relpath.split(os.sep)[0].lower()
    mtype = FOLDER_TO_TYPE.get(top)
    is_title = bool(mtype) and depth == TITLE_DEPTH[mtype] + 1
    return mtype, is_title


def _has_no_files(abspath):
    for _root, _dirs, files in os.walk(abspath):
        if files:
            return False
    return True


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(SQL_FILE.read_text())

        nodes = scan(BASE)
        await conn.execute("TRUNCATE fs_entries")
        rows = []
        for n in nodes:
            mtype, is_title = classify(n.path, n.depth)
            rows.append((n.path, n.name, norm(n.name), mtype, n.depth, is_title,
                         n.file_count, n.subtree_files, n.subtree_bytes, n.child_dirs))
        await conn.copy_records_to_table(
            "fs_entries",
            records=rows,
            columns=["path", "name", "name_norm", "media_type", "depth",
                     "is_title", "file_count", "subtree_files", "subtree_bytes",
                     "child_dirs"],
        )

        # --- duplicates: report only ---
        dupes = await conn.fetch("SELECT * FROM fn_duplicate_titles()")
        wasted = sum(d["wasted_bytes"] for d in dupes)
        print(f"Duplicate title groups: {len(dupes)}  (reclaimable ~{wasted/1e9:.1f} GB)")
        for d in dupes[:20]:
            print(f"  [{d['media_type']}] {d['name_norm']}: {d['copies']} copies"
                  f" — keep {d['keeper_path']}  (~{d['wasted_bytes']/1e9:.2f} GB extra)")

        # --- empties: auto-purge (deepest first), never a top-level type dir ---
        empties = await conn.fetch("SELECT * FROM fn_empty_folders()")
        purged = 0
        for row in empties:
            rel, depth = row["path"], row["depth"]
            if depth <= 1:                       # protect Movies/, TV/, etc.
                continue
            abspath = os.path.join(BASE, rel)
            if not os.path.isdir(abspath):
                continue
            if not _has_no_files(abspath):       # re-verify on disk before delete
                continue
            if PURGE:
                try:
                    shutil.rmtree(abspath)
                    purged += 1
                    print(f"  purged empty: {rel}")
                except OSError as ex:
                    print(f"  could not purge {rel}: {ex}")
        print(f"Empty folders found: {len(empties)}  purged: {purged}"
              f"  ({'live' if PURGE else 'dry-run'})")

        titles = sum(1 for n in nodes if classify(n.path, n.depth)[1])
        await conn.execute(
            """INSERT INTO audit_runs(dirs_scanned,titles,dup_groups,
                     empties_found,empties_purged,wasted_bytes)
               VALUES($1,$2,$3,$4,$5,$6)""",
            len(nodes), titles, len(dupes), len(empties), purged, int(wasted),
        )
        print(f"Audit complete: {len(nodes)} dirs, {titles} titles.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
