"""The lifecycle state machine — pure, DB-free, fully unit-tested.

This is the brains of L3: given a title's current row (or None) and an incoming
normalized event, decide what should happen (create / advance / backfill-ids /
nothing). The DB layer in projects/lifecycle.py just executes the Decision.

Two correctness properties that are easy to get wrong and so live here, tested:

  1. Monotonic progress under OUT-OF-ORDER delivery. Webhooks arrive out of order
     (a retried "Grab" can land after "Available"). State only ever moves FORWARD
     along the ladder; a lower-ranked event never regresses a title. Terminal side
     states (failed/deleted) are the deliberate exception — they always apply.

  2. Non-lifecycle events don't create rows. Auth successes, test pings, playback
     progress on nothing we track → target None → no row is conjured.

Ladder:  requested → grabbed → downloading → downloaded → available → played
Side:    failed, deleted   (can apply from anywhere; can be re-entered)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

LADDER = ["requested", "grabbed", "downloading", "downloaded", "available", "played"]
RANK = {s: i for i, s in enumerate(LADDER, start=1)}
TERMINAL_SIDE = {"failed", "deleted"}

# (source, event_type_lower) → target state. "*" source matches any (the *arr
# apps share Grab/Download/… verbs). Anything not here → None (not lifecycle-relevant).
_MAP: dict[tuple[str, str], str] = {
    # Jellyfin
    ("jellyfin", "itemadded"): "available",
    ("jellyfin", "playbackstart"): "played",
    ("jellyfin", "playbackstop"): "played",
    ("jellyfin", "playbackprogress"): "played",
    ("jellyfin", "play"): "played",
    ("jellyfin", "stop"): "played",
    # Sonarr / Radarr (shared verbs via the "*" wildcard)
    ("*", "grab"): "grabbed",
    ("*", "download"): "downloaded",     # arr "Download" == imported to disk
    ("*", "upgrade"): "downloaded",
    ("*", "rename"): "downloaded",
    ("*", "movieadded"): "requested",
    ("*", "seriesadd"): "requested",
    ("*", "moviedelete"): "deleted",
    ("*", "moviefiledelete"): "deleted",
    ("*", "seriesdelete"): "deleted",
    ("*", "episodefiledelete"): "deleted",
    # Jellyseerr
    ("jellyseerr", "media_pending"): "requested",
    ("jellyseerr", "media_approved"): "requested",
    ("jellyseerr", "media_auto_approved"): "requested",
    ("jellyseerr", "media_available"): "available",
    ("jellyseerr", "media_failed"): "failed",
    ("jellyseerr", "media_declined"): "failed",
}


def target_state(source: str, event_type: str) -> str | None:
    """The lifecycle state an event *wants* to move the title to, or None if the
    event isn't part of the pipeline (auth, test, unknown)."""
    s = (source or "").lower()
    et = (event_type or "").lower()
    return _MAP.get((s, et)) or _MAP.get(("*", et))


def decide(current: str | None, target: str | None) -> tuple[str | None, bool]:
    """(new_state, changed?) given the current and target states. Enforces the
    monotonic-forward + terminal-side rules described in the module docstring."""
    if target is None:
        return current, False
    if target in TERMINAL_SIDE:
        return (current, False) if current == target else (target, True)
    if current is None:
        return target, True
    if current in TERMINAL_SIDE:
        return target, True          # re-entering the pipeline after delete/fail
    if RANK.get(target, 0) > RANK.get(current, 0):
        return target, True
    return current, False            # same or earlier state → no-op (ignores late/dup events)


_ID_FIELDS = ("tmdb_id", "imdb_id", "tvdb_id")


@dataclass
class Decision:
    action: str                       # 'create' | 'advance' | 'merge_only' | 'none'
    new_state: str | None = None
    from_state: str | None = None
    record_transition: bool = False
    merged_ids: dict = field(default_factory=dict)   # ids to backfill onto the row
    title: str | None = None
    requested_by: str | None = None
    at: datetime | None = None
    event_type: str | None = None
    source: str | None = None
    note: str | None = None           # e.g. multi-match warning, for logs


def plan(current: dict | None, ev: dict) -> Decision:
    """Decide what the DB layer should do for one event. Pure — `current` is the
    matched lifecycle row as a dict (or None), `ev` is the normalized event."""
    source = ev.get("source")
    et = ev.get("event_type")
    target = target_state(source, et)
    at = ev.get("at")
    base = dict(at=at, event_type=et, source=source)

    # ids present on the event but missing/blank on the current row → backfill
    def new_ids():
        if current is None:
            return {k: ev.get(k) for k in _ID_FIELDS if ev.get(k)}
        return {k: ev.get(k) for k in _ID_FIELDS if ev.get(k) and not current.get(k)}

    if current is None:
        if target is None:
            return Decision(action="none", **base)     # don't create rows for non-pipeline events
        return Decision(action="create", new_state=target, from_state=None,
                        record_transition=True, merged_ids=new_ids(),
                        title=ev.get("title"), requested_by=ev.get("requested_by"), **base)

    new_state, changed = decide(current.get("state"), target)
    ids = new_ids()
    if changed:
        return Decision(action="advance", new_state=new_state, from_state=current.get("state"),
                        record_transition=True, merged_ids=ids,
                        title=ev.get("title"), requested_by=ev.get("requested_by"), **base)
    if ids or (ev.get("requested_by") and not current.get("requested_by")):
        return Decision(action="merge_only", new_state=current.get("state"),
                        merged_ids=ids, requested_by=ev.get("requested_by"), **base)
    return Decision(action="none", **base)
