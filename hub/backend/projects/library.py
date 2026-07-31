"""Library browse/search/detail endpoints backing the Hub "Catalog" view.

    GET /api/library/items    paginated/filtered/sorted browse
    GET /api/library/genres   live genre list (in-process TTL cache)
    GET /api/library/item     single item by id, for the detail modal

This is Phase 1 of "Jellyfin on the Hub site" — browse/search/detail only.
No auth, no playback: every route here is fully anonymous, same as every
other Hub endpoint, and reuses watching.py's _jf_get_async / _item_to_card /
degrade-never-500 idiom exactly rather than inventing a new pattern.
"""
import time

from fastapi import APIRouter

from projects.watching import JF_KEY, _item_to_card, _jf_get_async

router = APIRouter()

# Fields kept identical to latest()'s request so cards from /items, /item, and
# the landing rail are always shaped the same regardless of entry point.
_ITEM_FIELDS = (
    "Overview,Genres,ProductionYear,CommunityRating,"
    "CriticRating,OfficialRating,ProviderIds,People,RunTimeTicks"
)

_SORT_MAP = {
    "added": ("DateCreated", "Descending"),
    "title": ("SortName", "Ascending"),
    "year": ("ProductionYear", "Descending"),
    "rating": ("CommunityRating", "Descending"),
}

# Fallback IncludeItemTypes when a tab's library can't be resolved (see below).
_TYPE_MAP = {
    "all": "Movie,Series",
    "movie": "Movie",
    "series": "Series",
    "anime": "Movie,Series",
}

# This server separates anime as its own top-level LIBRARY (folder
# /data/media/Anime), not a genre — the anime items are tagged "Animation",
# same as Western animation. So the Movies / TV Shows / Anime tabs scope to
# their Jellyfin libraries (VirtualFolders) via ParentId, which is both correct
# and non-overlapping. Map our tab key -> the library's display Name.
_TYPE_LIBRARY = {"movie": "Movies", "series": "TV Shows", "anime": "Anime"}
_LIB_CACHE = {"data": None, "ts": 0.0}
_LIB_TTL = 900


async def _library_ids() -> dict:
    """{tab_type: library ItemId} from Jellyfin's VirtualFolders, cached.
    Looked up (not hardcoded) so it survives a library being recreated. Falls
    back to whatever's cached — or {} — if Jellyfin is unreachable."""
    now = time.time()
    if _LIB_CACHE["data"] is not None and now - _LIB_CACHE["ts"] < _LIB_TTL:
        return _LIB_CACHE["data"]
    try:
        folders = await _jf_get_async("/Library/VirtualFolders")
        by_name = {(v.get("Name") or "").strip().lower(): v.get("ItemId")
                   for v in (folders or [])}
        out = {t: by_name[name.lower()] for t, name in _TYPE_LIBRARY.items()
               if by_name.get(name.lower())}
        _LIB_CACHE["data"], _LIB_CACHE["ts"] = out, now
        return out
    except Exception:  # noqa: BLE001 — fall back to type-based filtering
        return _LIB_CACHE["data"] or {}


@router.get("/items")
async def items(q: str = "", type: str = "all", genre: str = "", sort: str = "added",
                 limit: int = 24, offset: int = 0):
    """Paginated/filtered/sorted browse of the Jellyfin library."""
    if not JF_KEY:
        return {"ok": False, "items": [], "total": 0, "offset": 0, "limit": 0, "has_more": False}

    limit = max(1, min(limit, 60))
    offset = max(0, offset)
    sort_by, sort_order = _SORT_MAP.get(sort, _SORT_MAP["added"])

    params = dict(
        SortBy=sort_by, SortOrder=sort_order,
        Recursive="true",
        StartIndex=offset, Limit=limit,
        Fields=_ITEM_FIELDS,
        ImageTypeLimit=1, EnableImageTypes="Primary",
    )
    # Movies / TV Shows / Anime tabs scope to their library (ParentId); "all"
    # (or a library we couldn't resolve) searches everything by item type.
    parent_id = (await _library_ids()).get(type) if type in _TYPE_LIBRARY else None
    if parent_id:
        params["ParentId"] = parent_id
        params["IncludeItemTypes"] = "Movie,Series"
    else:
        params["IncludeItemTypes"] = _TYPE_MAP.get(type, _TYPE_MAP["all"])

    q = (q or "").strip()
    if q:
        params["SearchTerm"] = q
    genre = (genre or "").strip()
    if genre:
        params["Genres"] = genre

    try:
        data = await _jf_get_async("/Items", **params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "items": [], "total": 0, "offset": offset,
                "limit": limit, "has_more": False}

    cards = [_item_to_card(it) for it in (data.get("Items") or [])]
    total = data.get("TotalRecordCount") or 0
    has_more = (offset + len(cards)) < total
    return {"ok": True, "items": cards, "total": total, "offset": offset,
            "limit": limit, "has_more": has_more}


# Live Jellyfin genre list, cached in-process (not the jf_item_genres Postgres
# table — that table is only ever populated as a side effect of the 5-minute
# /Sessions watch-poll catching an item mid-playback, so it can't supply a
# complete genre list). TTL keeps this off the hot path of every page load
# while still picking up newly-added genres within ~15 minutes.
_GENRE_CACHE_TTL = 900
_genre_cache = {"ts": 0.0, "genres": []}


@router.get("/genres")
async def genres():
    now = time.monotonic()
    if not JF_KEY:
        return {"ok": False, "genres": _genre_cache["genres"]}
    if _genre_cache["genres"] and (now - _genre_cache["ts"]) < _GENRE_CACHE_TTL:
        return {"ok": True, "genres": _genre_cache["genres"]}
    try:
        data = await _jf_get_async("/Genres", Recursive="true", IncludeItemTypes="Movie,Series")
        names = sorted({it.get("Name") for it in (data.get("Items") or []) if it.get("Name")})
    except Exception as e:  # noqa: BLE001
        # Serve stale cache rather than an empty list, if we have one.
        if _genre_cache["genres"]:
            return {"ok": True, "genres": _genre_cache["genres"], "stale": True}
        return {"ok": False, "error": str(e), "genres": []}
    _genre_cache["genres"] = names
    _genre_cache["ts"] = now
    return {"ok": True, "genres": names}


@router.get("/item")
async def item(id: str = ""):
    """Single item by id, for the catalog grid's detail modal."""
    if not (JF_KEY and id):
        return {"ok": False, "item": None}
    try:
        data = await _jf_get_async("/Items", Ids=id, Recursive="true", Fields=_ITEM_FIELDS,
                                    ImageTypeLimit=1, EnableImageTypes="Primary")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "item": None}
    found = (data.get("Items") or [])
    if not found:
        return {"ok": False, "item": None}
    return {"ok": True, "item": _item_to_card(found[0])}
