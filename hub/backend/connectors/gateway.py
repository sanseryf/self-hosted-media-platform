"""The Gateway facade — holds the four connectors and fans reads out concurrently.

Two responsibilities, both pure I/O aggregation (no lifecycle *interpretation* —
that's L3):
  • health()  — ping all four in parallel for the status page.
  • locate()  — given whatever external ids we have for a title, ask each system
                "what's your view of this?" concurrently, returning per-source
                MediaRefs. L3's lifecycle tracker turns those four views into one
                state machine.

A slow or dead upstream can't sink the whole call: each branch is isolated and a
failure becomes a null/HealthStatus(ok=False) for that source only.
"""
from __future__ import annotations

import asyncio

from .base import ConnectorError
from .jellyfin import JellyfinConnector
from .jellyseerr import JellyseerrConnector
from .models import HealthStatus, MediaRef
from .radarr import RadarrConnector
from .sonarr import SonarrConnector


class Gateway:
    def __init__(self, jellyfin, radarr, sonarr, jellyseerr):
        self.jellyfin: JellyfinConnector = jellyfin
        self.radarr: RadarrConnector = radarr
        self.sonarr: SonarrConnector = sonarr
        self.jellyseerr: JellyseerrConnector = jellyseerr
        self._all = (self.jellyfin, self.radarr, self.sonarr, self.jellyseerr)

    async def health(self) -> list[HealthStatus]:
        # health() never raises (it catches internally), so gather is enough.
        return list(await asyncio.gather(*(c.health() for c in self._all)))

    async def locate(self, *, tmdb=None, imdb=None, tvdb=None,
                     media_type: str | None = None) -> dict[str, MediaRef | None]:
        """Per-source view of one title. Each lookup is best-effort: a connector
        error (or disabled connector) yields None for that source rather than
        failing the whole locate()."""
        async def safe(coro):
            try:
                return await coro
            except ConnectorError:
                return None

        jf, ra, so, js = await asyncio.gather(
            safe(self.jellyfin.find_by_provider(tmdb=tmdb, imdb=imdb, tvdb=tvdb)),
            safe(self.radarr.movie_by_tmdb(tmdb)) if media_type != "series" else _none(),
            safe(self.sonarr.series_by_tvdb(tvdb)) if media_type != "movie" else _none(),
            safe(self.jellyseerr.media_by_tmdb(tmdb, media_type)),
        )
        return {"jellyfin": jf, "radarr": ra, "sonarr": so, "jellyseerr": js}

    async def snapshot(self) -> dict:
        """Full-library snapshot for the reconciler: Jellyfin items + combined
        Radarr/Sonarr records, each with a success flag. Reconcile MUST check the
        flags — diffing against a failed (empty) enumeration would invent false
        drift, so a failed source is reported, not silently treated as empty."""
        jf, mv, sr = await asyncio.gather(
            self.jellyfin.list_library(), self.radarr.list_movies(), self.sonarr.list_series(),
            return_exceptions=True,
        )
        def ok(v):
            return not isinstance(v, Exception)
        return {
            "jellyfin": {"ok": ok(jf), "items": jf if ok(jf) else [],
                         "error": None if ok(jf) else str(jf)},
            "arr": {"ok": ok(mv) and ok(sr),
                    "items": (mv if ok(mv) else []) + (sr if ok(sr) else []),
                    "error": None if (ok(mv) and ok(sr)) else str(mv if not ok(mv) else sr)},
        }

    async def aclose(self) -> None:
        await asyncio.gather(*(c.aclose() for c in self._all), return_exceptions=True)


async def _none():
    return None
