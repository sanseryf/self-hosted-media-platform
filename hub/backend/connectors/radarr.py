"""Radarr connector — movie state: monitored / downloading / on disk.

Error points handled here:
  • Auth header is X-Api-Key; API base is /api/v3.
  • GET /movie?tmdbId=<id> returns ONLY movies already in the library — a
    not-yet-added movie comes back as an empty list, not a 404. So "empty list"
    means "absent", and we must not treat it as an error.
  • ids (tmdbId) are ints in the payload → coerced to str for correlation.
"""
from __future__ import annotations

from .base import HttpConnector
from .models import MediaRef, SystemState, as_id


class RadarrConnector(HttpConnector):
    name = "radarr"
    api_base = "/api/v3"
    _ping_path = "/system/status"

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def movie_by_tmdb(self, tmdb) -> MediaRef | None:
        if not self.enabled or not tmdb:
            return None
        data = await self._get_json("/movie", {"tmdbId": tmdb})
        # Radarr returns a (possibly empty) list; empty == not in library == absent.
        if not isinstance(data, list) or not data:
            return None
        m = data[0]
        has_file = bool(m.get("hasFile"))
        monitored = bool(m.get("monitored"))
        if has_file:
            state = SystemState.downloaded
        elif monitored:
            state = SystemState.monitored
        else:
            state = SystemState.absent
        return MediaRef(
            source="radarr", state=state,
            system_id=as_id(m.get("id")),
            tmdb_id=as_id(m.get("tmdbId")), imdb_id=as_id(m.get("imdbId")),
            title=m.get("title"), media_type="movie",
            extra={"hasFile": has_file, "monitored": monitored,
                   "sizeOnDisk": m.get("sizeOnDisk"), "status": m.get("status")},
        )

    async def list_movies(self) -> list[dict]:
        """Every library movie, shaped for reconcile_diff. Raises on error."""
        if not self.enabled:
            return []
        data = await self._get_json("/movie")
        rows = data if isinstance(data, list) else []
        return [{
            "system_id": as_id(m.get("id")), "title": m.get("title"), "media_type": "movie",
            "tmdb_id": as_id(m.get("tmdbId")), "imdb_id": as_id(m.get("imdbId")), "tvdb_id": None,
            "has_file": bool(m.get("hasFile")), "monitored": bool(m.get("monitored")),
        } for m in rows]

    async def diskspace(self) -> list[dict]:
        """Free space per root (for the policy engine's disk gate). [] on disable."""
        if not self.enabled:
            return []
        data = await self._get_json("/diskspace")
        return data if isinstance(data, list) else []

    async def queue(self) -> list[dict]:
        """Active download queue records (title is 'queued'/downloading)."""
        if not self.enabled:
            return []
        data = await self._get_json("/queue")
        # Radarr wraps queue in {records: [...]}; older shapes returned a bare list.
        if isinstance(data, dict):
            return data.get("records") or []
        return data if isinstance(data, list) else []
