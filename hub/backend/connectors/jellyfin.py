"""Jellyfin connector — "does the library actually have this, and who's watching?"

Error points handled here:
  • Auth is a header (X-Emby-Token), NOT a query param — keeps the admin key out
    of URLs and access logs. Jellyfin also accepts ?api_key=, but header is cleaner.
  • Jellyfin endpoints have NO /api/vN prefix (api_base=""), unlike the *arr apps.
  • Lookup-by-external-id uses ?AnyProviderIdEquals=<provider>.<id> with the
    provider name LOWERCASED (tmdb./imdb./tvdb.), even though ProviderIds come back
    Capitalized (Tmdb/Imdb/Tvdb). Getting that casing wrong silently returns 0 hits.
"""
from __future__ import annotations

from .base import ConnectorNotFound, HttpConnector
from .models import MediaRef, SystemState, as_id


class JellyfinConnector(HttpConnector):
    name = "jellyfin"
    api_base = ""
    _ping_path = "/System/Info"

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self.api_key}

    async def find_by_provider(self, *, tmdb=None, imdb=None, tvdb=None) -> MediaRef | None:
        """Return Jellyfin's view of a title identified by an external id, or None
        if it isn't in the library. Presence in Jellyfin == available to watch."""
        if not self.enabled:
            return None
        # priority tmdb > imdb > tvdb; provider name must be lowercased for the query
        for provider, value in (("tmdb", tmdb), ("imdb", imdb), ("tvdb", tvdb)):
            if value:
                needle = f"{provider}.{value}"
                break
        else:
            return None
        try:
            data = await self._get_json("/Items", {
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "AnyProviderIdEquals": needle,
                "Fields": "ProviderIds",
                "Limit": 1,
            })
        except ConnectorNotFound:
            return None
        items = (data or {}).get("Items") or []
        if not items:
            return None
        it = items[0]
        pid = it.get("ProviderIds") or {}
        return MediaRef(
            source="jellyfin", state=SystemState.available,
            system_id=as_id(it.get("Id")),
            tmdb_id=as_id(pid.get("Tmdb")), imdb_id=as_id(pid.get("Imdb")),
            tvdb_id=as_id(pid.get("Tvdb")),
            title=it.get("Name"),
            media_type=(it.get("Type") or "").lower() or None,
        )

    async def sessions(self) -> list[dict]:
        """Raw active sessions (for the L4 live wall). Empty list if disabled."""
        if not self.enabled:
            return []
        data = await self._get_json("/Sessions")
        return data if isinstance(data, list) else []

    async def list_library(self, page: int = 1000) -> list[dict]:
        """Every movie + series with its provider ids, paginated. Shaped for
        reconcile_diff (title/media_type/tmdb_id/imdb_id/tvdb_id). Raises on error
        so the reconciler can abort rather than diff against a partial snapshot."""
        if not self.enabled:
            return []
        out: list[dict] = []
        start = 0
        while True:
            data = await self._get_json("/Items", {
                "Recursive": "true", "IncludeItemTypes": "Movie,Series",
                "Fields": "ProviderIds", "StartIndex": start, "Limit": page,
                "EnableTotalRecordCount": "false",
            })
            items = (data or {}).get("Items") or []
            for it in items:
                pid = it.get("ProviderIds") or {}
                out.append({
                    "system_id": as_id(it.get("Id")),
                    "title": it.get("Name"),
                    "media_type": (it.get("Type") or "").lower() or None,
                    "tmdb_id": as_id(pid.get("Tmdb")), "imdb_id": as_id(pid.get("Imdb")),
                    "tvdb_id": as_id(pid.get("Tvdb")),
                })
            if len(items) < page:
                break
            start += page
        return out
