"""Sonarr connector — series state: monitored / has episodes on disk.

Error points handled here:
  • Auth header X-Api-Key; API base /api/v3.
  • GET /series?tvdbId=<id> returns only library series ([] == absent, not 404).
    Sonarr keys on TVDB, not TMDB — so the correlation id from a Sonarr event is
    tvdb_id, and Jellyseerr/Jellyfin must be joined via tvdb where possible.
  • "downloaded" for a series is fuzzy: we call it downloaded once ANY episode
    file exists (statistics.episodeFileCount > 0); L3 decides completeness.
"""
from __future__ import annotations

from .base import HttpConnector
from .models import MediaRef, SystemState, as_id


class SonarrConnector(HttpConnector):
    name = "sonarr"
    api_base = "/api/v3"
    _ping_path = "/system/status"

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def series_by_tvdb(self, tvdb) -> MediaRef | None:
        if not self.enabled or not tvdb:
            return None
        data = await self._get_json("/series", {"tvdbId": tvdb})
        if not isinstance(data, list) or not data:
            return None
        s = data[0]
        stats = s.get("statistics") or {}
        file_count = stats.get("episodeFileCount") or 0
        monitored = bool(s.get("monitored"))
        if file_count > 0:
            state = SystemState.downloaded
        elif monitored:
            state = SystemState.monitored
        else:
            state = SystemState.absent
        return MediaRef(
            source="sonarr", state=state,
            system_id=as_id(s.get("id")),
            tvdb_id=as_id(s.get("tvdbId")), imdb_id=as_id(s.get("imdbId")),
            tmdb_id=as_id(s.get("tmdbId")),
            title=s.get("title"), media_type="series",
            extra={"monitored": monitored, "episodeFileCount": file_count,
                   "episodeCount": stats.get("episodeCount"),
                   "sizeOnDisk": stats.get("sizeOnDisk"), "status": s.get("status")},
        )

    async def list_series(self) -> list[dict]:
        """Every library series, shaped for reconcile_diff (has_file == any episode
        on disk). Raises on error so the reconciler can abort on a partial snapshot."""
        if not self.enabled:
            return []
        data = await self._get_json("/series")
        rows = data if isinstance(data, list) else []
        out = []
        for s in rows:
            stats = s.get("statistics") or {}
            out.append({
                "system_id": as_id(s.get("id")), "title": s.get("title"), "media_type": "series",
                "tvdb_id": as_id(s.get("tvdbId")), "imdb_id": as_id(s.get("imdbId")),
                "tmdb_id": as_id(s.get("tmdbId")),
                "has_file": (stats.get("episodeFileCount") or 0) > 0,
                "monitored": bool(s.get("monitored")),
            })
        return out

    async def queue(self) -> list[dict]:
        if not self.enabled:
            return []
        data = await self._get_json("/queue")
        if isinstance(data, dict):
            return data.get("records") or []
        return data if isinstance(data, list) else []
