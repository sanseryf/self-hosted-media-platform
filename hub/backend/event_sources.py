"""Pure, DB-free normalization of inbound webhook payloads into one envelope.

Each of the four upstreams speaks its own JSON dialect:
  • Jellyfin (webhook plugin)  — PascalCase, provider ids flattened as Provider_<Name>
  • Sonarr / Radarr            — camelCase, nested `series` / `movie` objects
  • Jellyseerr                 — snake_case, nested `media` / `request` objects

`extract(source, payload)` collapses all of them into an `Envelope` carrying the
fields L1 stores and L3 later joins on, or raises `UnknownEvent` when the payload
doesn't look like anything we recognize (the caller dead-letters those).

This module is deliberately import-clean — stdlib only, no FastAPI, no asyncpg —
so the single trickiest part of L1 (parsing four payload shapes) is unit-testable
with no database and no running server. See tests/test_event_sources.py.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

SOURCES = ("jellyfin", "sonarr", "radarr", "jellyseerr")


class UnknownEvent(ValueError):
    """Payload is valid JSON but not a shape we know how to normalize."""


@dataclass
class Envelope:
    source: str
    event_type: str
    dedup_key: str
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    title: str | None = None
    media_type: str | None = None
    user_name: str | None = None
    occurred_at: datetime | None = None


# ─────────────────────────────────────────────────────────── helpers ──────────
def _s(v) -> str | None:
    """Coerce ids that arrive as ints (arr/seerr tmdbId) or strings to a stable
    str, normalizing "" and None to None so they don't pollute a dedup key."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _key(*parts) -> str:
    """Deterministic idempotency key from the canonical event tuple. Positions
    are fixed (a missing part becomes "") so the same logical event always hashes
    the same, and different events practically never collide. Hashed rather than
    stored raw so the key is a bounded, index-friendly, PII-free 64 hex chars."""
    canon = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


_FRAC = re.compile(r"(\.\d{6})\d+")


def _parse_ts(v) -> datetime | None:
    """Best-effort ISO-8601 → tz-aware datetime. Tolerates Jellyfin's 7-digit
    fractional seconds (fromisoformat only takes 3 or 6) and a trailing Z, and
    assumes UTC for a naive stamp so asyncpg can bind it to a timestamptz.
    Returns None on anything it can't parse — occurred_at is optional; received_at
    is always there as the fallback ordering key."""
    if not v or not isinstance(v, str):
        return None
    s = v.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = _FRAC.sub(r"\1", s)  # truncate over-long fractional seconds
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ───────────────────────────────────────────────────── per-source ─────────────
def _extract_jellyfin(p: dict) -> Envelope:
    et = p.get("NotificationType") or p.get("notificationType")
    if not et:
        raise UnknownEvent("no NotificationType (not a Jellyfin webhook payload?)")
    # The webhook plugin flattens ProviderIds into Provider_<Name> keys; casing of
    # the suffix has varied across plugin versions, so accept both.
    imdb = _s(p.get("Provider_Imdb") or p.get("Provider_imdb"))
    tmdb = _s(p.get("Provider_Tmdb") or p.get("Provider_tmdb"))
    tvdb = _s(p.get("Provider_Tvdb") or p.get("Provider_tvdb"))
    item_type = _s(p.get("ItemType"))
    user = _s(p.get("NotificationUsername") or p.get("Username") or p.get("UserId"))
    occurred = _parse_ts(p.get("UtcTimestamp") or p.get("Timestamp"))
    # Dedup on the tuple that makes a playback/library event unique. PositionTicks
    # discriminates repeated PlaybackProgress beats for the same item+user; the
    # timestamp discriminates otherwise-identical Start/Stop pairs.
    dedup = _key("jellyfin", et, p.get("ItemId"), user,
                 p.get("PlaybackPositionTicks"), p.get("UtcTimestamp"))
    return Envelope(
        source="jellyfin", event_type=str(et), dedup_key=dedup,
        imdb_id=imdb, tmdb_id=tmdb, tvdb_id=tvdb,
        title=_s(p.get("SeriesName") or p.get("Name")),
        media_type=(item_type.lower() if item_type else None),
        user_name=user, occurred_at=occurred,
    )


def _extract_arr(source: str, p: dict) -> Envelope:
    et = p.get("eventType")
    if not et:
        raise UnknownEvent("no eventType (not a Sonarr/Radarr webhook payload?)")
    download_id = _s(p.get("downloadId"))
    if source == "radarr":
        m = p.get("movie") or {}
        tmdb, imdb, tvdb = _s(m.get("tmdbId")), _s(m.get("imdbId")), None
        title, media_type = _s(m.get("title")), "movie"
        mf = p.get("movieFile") or {}
        # relativePath makes an import unique when downloadId is absent (manual imports).
        extra = _s(mf.get("relativePath"))
    else:  # sonarr
        s = p.get("series") or {}
        tmdb, imdb, tvdb = _s(s.get("tmdbId")), _s(s.get("imdbId")), _s(s.get("tvdbId"))
        title, media_type = _s(s.get("title")), "series"
        eps = p.get("episodes") or []
        ep_ids = sorted(str(e.get("id")) for e in eps if e.get("id") is not None)
        extra = ",".join(ep_ids) or None
    dedup = _key(source, et, tmdb, tvdb, imdb, download_id, extra)
    return Envelope(
        source=source, event_type=str(et), dedup_key=dedup,
        imdb_id=imdb, tmdb_id=tmdb, tvdb_id=tvdb,
        title=title, media_type=media_type, user_name=None, occurred_at=None,
    )


def _extract_jellyseerr(p: dict) -> Envelope:
    et = p.get("notification_type")
    if not et:
        raise UnknownEvent("no notification_type (not a Jellyseerr webhook payload?)")
    media = p.get("media") or {}
    request = p.get("request") or {}
    tmdb = _s(media.get("tmdbId"))
    tvdb = _s(media.get("tvdbId"))
    # requestedBy_username appears at top level in some templates, nested in others.
    user = _s(request.get("requestedBy_username") or p.get("requestedBy_username"))
    req_id = _s(request.get("request_id") or p.get("request_id"))
    dedup = _key("jellyseerr", et, tmdb, tvdb, req_id, _s(p.get("subject")))
    return Envelope(
        source="jellyseerr", event_type=str(et), dedup_key=dedup,
        imdb_id=None, tmdb_id=tmdb, tvdb_id=tvdb,
        title=_s(p.get("subject")),
        media_type=_s(media.get("media_type")),
        user_name=user, occurred_at=None,
    )


def extract(source: str, payload: dict) -> Envelope:
    """Dispatch to the right per-source parser. Raises UnknownEvent for an
    unknown source or an unrecognizable payload — the caller turns that into a
    dead-letter row rather than a lost event."""
    if not isinstance(payload, dict):
        raise UnknownEvent("payload is not a JSON object")
    source = (source or "").lower()
    if source == "jellyfin":
        return _extract_jellyfin(payload)
    if source in ("sonarr", "radarr"):
        return _extract_arr(source, payload)
    if source == "jellyseerr":
        return _extract_jellyseerr(payload)
    raise UnknownEvent(f"unknown source '{source}'")
