"""Unit tests for event_sources.extract — the four payload parsers.

DB-free and server-free on purpose: these cover the part of L1 most likely to be
wrong (four different upstream JSON shapes), so they must run anywhere Python does.

Run:  python hub/backend/tests/test_event_sources.py     (self-contained), or
      pytest hub/backend/tests/                            (if pytest is present)
"""
import sys
from pathlib import Path

# Make `import event_sources` work whether run via pytest or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import event_sources as es  # noqa: E402

# ── representative payloads (trimmed to the fields the extractors read) ────────
JELLYFIN_PLAYBACK = {
    "NotificationType": "PlaybackStart",
    "ItemId": "abc123", "ItemType": "Movie", "Name": "Starfall Protocol",
    "Provider_Imdb": "tt1160419", "Provider_Tmdb": "438631",
    "NotificationUsername": "alice",
    "PlaybackPositionTicks": 0,
    "UtcTimestamp": "2026-07-30T18:04:11.1234567Z",  # 7-digit fraction + Z (the tricky case)
}
JELLYFIN_EPISODE = {
    "NotificationType": "PlaybackStop", "ItemId": "ep9", "ItemType": "Episode",
    "Name": "Pilot", "SeriesName": "Glasshouse", "Provider_Tvdb": "371980",
    "NotificationUsername": "guest",
}
RADARR_IMPORT = {
    "eventType": "Download",
    "movie": {"id": 12, "title": "Hollow Creek", "year": 2025,
              "tmdbId": 1233413, "imdbId": "tt31193180"},
    "movieFile": {"relativePath": "Hollow Creek (2025).mkv"},
    "downloadId": "ABCDEF0123",
}
SONARR_GRAB = {
    "eventType": "Grab",
    "series": {"id": 5, "title": "The Long Meridian", "tvdbId": 368211, "imdbId": "tt9253284"},
    "episodes": [{"id": 902, "seasonNumber": 2, "episodeNumber": 1},
                 {"id": 901, "seasonNumber": 2, "episodeNumber": 2}],
    "downloadId": "XYZ999",
}
JELLYSEERR_APPROVED = {
    "notification_type": "MEDIA_APPROVED",
    "subject": "Hollow Creek (2025)",
    "media": {"media_type": "movie", "tmdbId": 1233413, "status": "PROCESSING"},
    "request": {"request_id": "77", "requestedBy_username": "alice"},
}


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_jellyfin_movie():
    e = es.extract("jellyfin", JELLYFIN_PLAYBACK)
    _check(e.source == "jellyfin" and e.event_type == "PlaybackStart", "jf type")
    _check(e.tmdb_id == "438631" and e.imdb_id == "tt1160419", "jf ids")
    _check(e.media_type == "movie", "jf media_type lowercased")
    _check(e.user_name == "alice", "jf user")
    # the 7-digit fractional + Z timestamp must parse to a tz-aware datetime
    _check(e.occurred_at is not None and e.occurred_at.tzinfo is not None, "jf ts parsed tz-aware")


def test_jellyfin_episode_prefers_series_title():
    e = es.extract("jellyfin", JELLYFIN_EPISODE)
    _check(e.title == "Glasshouse", "episode should carry the series title")
    _check(e.media_type == "episode" and e.tvdb_id == "371980", "episode fields")


def test_radarr_movie():
    e = es.extract("radarr", RADARR_IMPORT)
    _check(e.source == "radarr" and e.event_type == "Download", "radarr type")
    _check(e.tmdb_id == "1233413" and e.imdb_id == "tt31193180", "radarr ids as str")
    _check(e.media_type == "movie" and e.title == "Hollow Creek", "radarr fields")


def test_sonarr_series():
    e = es.extract("sonarr", SONARR_GRAB)
    _check(e.source == "sonarr" and e.event_type == "Grab", "sonarr type")
    _check(e.tvdb_id == "368211" and e.media_type == "series", "sonarr fields")


def test_jellyseerr_request():
    e = es.extract("jellyseerr", JELLYSEERR_APPROVED)
    _check(e.event_type == "MEDIA_APPROVED", "seerr type")
    _check(e.tmdb_id == "1233413" and e.media_type == "movie", "seerr fields")
    _check(e.user_name == "alice" and e.title == "Hollow Creek (2025)", "seerr user/title")


def test_dedup_is_deterministic():
    a = es.extract("radarr", RADARR_IMPORT).dedup_key
    b = es.extract("radarr", RADARR_IMPORT).dedup_key
    _check(a == b and len(a) == 64, "same payload → same 64-hex key")


def test_dedup_distinguishes_events():
    # Sonarr episode-list order must not change the key, but a different event does.
    reordered = dict(SONARR_GRAB, episodes=list(reversed(SONARR_GRAB["episodes"])))
    _check(es.extract("sonarr", SONARR_GRAB).dedup_key
           == es.extract("sonarr", reordered).dedup_key, "episode order must not matter")
    other = dict(SONARR_GRAB, eventType="Download")
    _check(es.extract("sonarr", SONARR_GRAB).dedup_key
           != es.extract("sonarr", other).dedup_key, "different eventType → different key")


def test_unknown_shapes_raise():
    for src, bad in (("jellyfin", {"foo": 1}), ("radarr", {"foo": 1}),
                     ("jellyseerr", {"foo": 1}), ("nope", {"eventType": "x"})):
        try:
            es.extract(src, bad)
        except es.UnknownEvent:
            continue
        raise AssertionError(f"{src} unknown payload should have raised UnknownEvent")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed.")
