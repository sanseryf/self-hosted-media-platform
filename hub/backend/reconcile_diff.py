"""Pure three-way reconciliation logic — DB-free, unit-tested.

The invariant we check: every title on disk (an *arr record with a file) should be
visible in Jellyfin, and every Jellyfin item should have an *arr record backing it.
Drift in either direction is a finding.

  • orphan_in_jellyfin  — Jellyfin has it, no *arr record matches → who's managing this?
  • missing_from_jellyfin — *arr has the file, Jellyfin never indexed it → import/scan gap
  • arr_wanted           — *arr monitors it but has no file (informational: a gap, not drift)

"Disk" is proxied by *arr's `has_file`, because the hub container doesn't mount the
library — an honest limitation, not a filesystem stat. Matching is by ANY shared
external id (tmdb/imdb/tvdb), with a media_type guard so a movie and a series that
happen to share a numeric id don't cross-match.
"""
from __future__ import annotations

_ID_FIELDS = ("tmdb_id", "imdb_id", "tvdb_id")


def _id_keys(item: dict) -> set[tuple[str, str]]:
    """The (provider, id) pairs this item can be matched on, media-type-scoped so
    movie:123 never collides with series:123."""
    mt = (item.get("media_type") or "").lower() or "?"
    return {(f"{mt}:{f}", str(item[f])) for f in _ID_FIELDS if item.get(f)}


def _index(items: list[dict]) -> dict[tuple[str, str], dict]:
    idx: dict[tuple[str, str], dict] = {}
    for it in items:
        for key in _id_keys(it):
            idx.setdefault(key, it)
    return idx


def _matches(item: dict, index: dict[tuple[str, str], dict]) -> bool:
    return any(key in index for key in _id_keys(item))


def diff(jellyfin: list[dict], arr: list[dict]) -> dict:
    """Compare a Jellyfin library snapshot against the combined Radarr+Sonarr
    snapshot. Each list holds dicts with tmdb_id/imdb_id/tvdb_id/title/media_type;
    *arr items additionally carry `has_file` and `monitored`.

    Returns findings + summary counts. Pure and order-independent.
    """
    jf_index = _index(jellyfin)
    arr_index = _index(arr)

    orphan_in_jellyfin = [
        {"title": it.get("title"), "media_type": it.get("media_type"),
         "ids": {k: it.get(k) for k in _ID_FIELDS}}
        for it in jellyfin if not _matches(it, arr_index)
    ]

    missing_from_jellyfin = [
        {"title": it.get("title"), "media_type": it.get("media_type"),
         "ids": {k: it.get(k) for k in _ID_FIELDS}}
        for it in arr if it.get("has_file") and not _matches(it, jf_index)
    ]

    arr_wanted = [
        {"title": it.get("title"), "media_type": it.get("media_type"),
         "ids": {k: it.get(k) for k in _ID_FIELDS}}
        for it in arr if it.get("monitored") and not it.get("has_file")
    ]

    return {
        "summary": {
            "jellyfin_items": len(jellyfin),
            "arr_items": len(arr),
            "orphan_in_jellyfin": len(orphan_in_jellyfin),
            "missing_from_jellyfin": len(missing_from_jellyfin),
            "arr_wanted": len(arr_wanted),
        },
        "orphan_in_jellyfin": orphan_in_jellyfin,
        "missing_from_jellyfin": missing_from_jellyfin,
        "arr_wanted": arr_wanted,
    }
