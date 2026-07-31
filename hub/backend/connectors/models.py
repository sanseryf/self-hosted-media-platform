"""Normalized types the gateway speaks — the single contract L3 reads instead of
four upstream dialects.

`SystemState` is the per-system lifecycle position of a title *within one system*
(Jellyseerr says requested/processing, an *arr says monitored/downloaded, Jellyfin
says available). L3's lifecycle tracker composes these four per-system states into
the overall request→available state machine; here we just report each honestly.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


def as_id(v) -> str | None:
    """Coerce an id that may arrive as int (arr/seerr tmdbId) or str to a stable
    str, normalizing None/"" to None so empty ids don't masquerade as real ones."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


class SystemState(str, Enum):
    absent = "absent"          # the system has never heard of this title
    requested = "requested"    # Jellyseerr: pending approval
    monitored = "monitored"    # *arr: added + monitored, no file yet
    queued = "queued"          # actively downloading / processing
    downloaded = "downloaded"  # *arr: file on disk / Jellyseerr: partial
    available = "available"    # Jellyfin has it / Jellyseerr: fully available
    unknown = "unknown"        # reachable but state couldn't be determined


class MediaRef(BaseModel):
    """One system's view of one title."""
    source: str                                  # jellyfin | radarr | sonarr | jellyseerr
    state: SystemState
    system_id: str | None = None                 # the id *within* that system (Jellyfin ItemId, Radarr movieId, …)
    tmdb_id: str | None = None
    imdb_id: str | None = None
    tvdb_id: str | None = None
    title: str | None = None
    media_type: str | None = None                # movie | series | episode
    extra: dict = Field(default_factory=dict)    # source-specific detail (progress, quality, size, path…)


class HealthStatus(BaseModel):
    source: str
    ok: bool
    detail: str | None = None
    latency_ms: int | None = None
