"""Jellyseerr connector — request state: pending / processing / available.

Error points handled here:
  • Auth header X-Api-Key; API base /api/v1.
  • Lookup is by media TYPE + tmdbId: GET /movie/{tmdbId} or /tv/{tmdbId}. Passing
    a tv id to /movie (or vice-versa) yields the wrong record or a 404 — so the
    caller MUST pass media_type. Unknown/missing type → we can't look it up → None.
  • A title TMDB knows about but nobody requested comes back 200 with
    mediaInfo=null → that's `absent` here, not an error.
  • mediaInfo.status is an int enum (1 none, 2 pending, 3 processing, 4 partial,
    5 available); we map it to SystemState.
"""
from __future__ import annotations

from .base import ConnectorNotFound, HttpConnector
from .models import MediaRef, SystemState, as_id

# Jellyseerr/Overseerr mediaInfo.status → our per-system state.
_STATUS = {
    1: SystemState.absent,       # none
    2: SystemState.requested,    # pending approval
    3: SystemState.queued,       # processing (being fetched)
    4: SystemState.downloaded,   # partially available
    5: SystemState.available,    # fully available
}


class JellyseerrConnector(HttpConnector):
    name = "jellyseerr"
    api_base = "/api/v1"
    _ping_path = "/status"

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def media_by_tmdb(self, tmdb, media_type: str | None) -> MediaRef | None:
        if not self.enabled or not tmdb:
            return None
        mt = (media_type or "").lower()
        if mt in ("movie", "film"):
            path = f"/movie/{tmdb}"
        elif mt in ("series", "tv", "show"):
            path = f"/tv/{tmdb}"
        else:
            # Without a type we can't pick the right endpoint — don't guess.
            return None
        try:
            data = await self._get_json(path)
        except ConnectorNotFound:
            return None
        info = (data or {}).get("mediaInfo")
        if not info:
            # Known to TMDB, but no request/media record here yet.
            return MediaRef(source="jellyseerr", state=SystemState.absent,
                            tmdb_id=as_id(tmdb), media_type=mt or None,
                            title=data.get("title") or data.get("name"))
        state = _STATUS.get(info.get("status"), SystemState.unknown)
        return MediaRef(
            source="jellyseerr", state=state,
            system_id=as_id(info.get("id")),
            tmdb_id=as_id(tmdb), tvdb_id=as_id(info.get("tvdbId")),
            imdb_id=as_id(info.get("imdbId")),
            title=data.get("title") or data.get("name"),
            media_type=mt or None,
            extra={"status_code": info.get("status")},
        )

    async def pending_count(self, requested_by: str | None = None) -> int | None:
        """Number of pending requests, optionally scoped to one requester (matched
        by username). Best-effort context for the policy quota gate — returns None
        (gate skipped) if it can't be determined. Never raises to the caller."""
        if not self.enabled:
            return None
        try:
            data = await self._get_json("/request", {"filter": "pending", "take": 100, "skip": 0})
        except Exception:  # noqa: BLE001
            return None
        results = (data or {}).get("results") or []
        if not requested_by:
            return len(results)
        n = 0
        for r in results:
            u = r.get("requestedBy") or {}
            if requested_by in (u.get("username"), u.get("jellyfinUsername"),
                                u.get("plexUsername"), u.get("displayName")):
                n += 1
        return n

    async def approve_request(self, request_id) -> dict:
        """Approve a pending request. WRITE — single attempt, no retry. Only ever
        called by the policy engine when POLICY_ENFORCE is on. NOTE: verify this
        endpoint against your Jellyseerr version before enabling enforcement."""
        return await self._post_once(f"/request/{request_id}/approve")
