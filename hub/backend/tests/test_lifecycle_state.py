"""Unit tests for the lifecycle state machine (pure, DB-free).

Covers the two properties most likely to be wrong: monotonic progress under
out-of-order delivery, and not creating rows for non-pipeline events.

Run:  python hub/backend/tests/test_lifecycle_state.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lifecycle_state as L  # noqa: E402

results = []
def check(name, cond):
    results.append(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def test_target_mapping():
    check("jellyseerr pending -> requested", L.target_state("jellyseerr", "MEDIA_PENDING") == "requested")
    check("arr Grab -> grabbed", L.target_state("radarr", "Grab") == "grabbed")
    check("arr Download -> downloaded", L.target_state("sonarr", "Download") == "downloaded")
    check("jellyfin ItemAdded -> available", L.target_state("jellyfin", "ItemAdded") == "available")
    check("jellyfin PlaybackStart -> played", L.target_state("jellyfin", "PlaybackStart") == "played")
    check("arr delete -> deleted", L.target_state("radarr", "MovieFileDelete") == "deleted")
    check("jellyseerr failed -> failed", L.target_state("jellyseerr", "MEDIA_FAILED") == "failed")
    check("irrelevant event -> None", L.target_state("jellyfin", "AuthenticationSuccess") is None)
    check("arr Test -> None", L.target_state("radarr", "Test") is None)


def test_decide_advances_forward_only():
    check("requested -> grabbed advances", L.decide("requested", "grabbed") == ("grabbed", True))
    check("grabbed -> available advances", L.decide("grabbed", "available") == ("available", True))
    # OUT OF ORDER: a late 'grabbed' after 'available' must NOT regress
    check("available + late grabbed = no regress", L.decide("available", "grabbed") == ("available", False))
    check("played + late download = no regress", L.decide("played", "downloaded") == ("played", False))
    check("same state = no-op", L.decide("downloaded", "downloaded") == ("downloaded", False))


def test_decide_terminal_sides():
    check("available -> deleted applies", L.decide("available", "deleted") == ("deleted", True))
    check("requested -> failed applies", L.decide("requested", "failed") == ("failed", True))
    check("deleted then re-grabbed re-enters", L.decide("deleted", "grabbed") == ("grabbed", True))
    check("deleted + deleted = no dup", L.decide("deleted", "deleted") == ("deleted", False))


def test_plan_create_only_for_pipeline_events():
    # non-pipeline event with no existing row -> create nothing
    d = L.plan(None, {"source": "jellyfin", "event_type": "AuthenticationSuccess"})
    check("no row + irrelevant event -> none", d.action == "none")

    d = L.plan(None, {"source": "jellyseerr", "event_type": "MEDIA_PENDING",
                      "tmdb_id": "438631", "media_type": "movie", "title": "Starfall Protocol",
                      "requested_by": "alice"})
    check("no row + request -> create requested", d.action == "create" and d.new_state == "requested")
    check("create carries ids + requester", d.merged_ids.get("tmdb_id") == "438631" and d.requested_by == "alice")


def test_plan_advance_and_stitch():
    current = {"state": "requested", "tmdb_id": "438631", "imdb_id": None, "tvdb_id": None,
               "title": "Starfall Protocol", "requested_by": "alice"}
    # a Grab event that also teaches us the imdb id
    d = L.plan(current, {"source": "radarr", "event_type": "Grab",
                         "tmdb_id": "438631", "imdb_id": "tt1160419"})
    check("advance requested->grabbed", d.action == "advance" and d.new_state == "grabbed")
    check("advance backfills new imdb id", d.merged_ids.get("imdb_id") == "tt1160419")

    # a late duplicate that only carries a known id -> nothing to do
    d2 = L.plan({"state": "available", "tmdb_id": "438631", "imdb_id": "tt1160419", "tvdb_id": None},
                {"source": "radarr", "event_type": "Grab", "tmdb_id": "438631"})
    check("late dup with no new info -> none", d2.action == "none")

    # merge-only: no state change but a new id arrives
    d3 = L.plan({"state": "available", "tmdb_id": "438631", "imdb_id": None, "tvdb_id": None},
                {"source": "jellyfin", "event_type": "ItemAdded", "tmdb_id": "438631",
                 "imdb_id": "tt1160419"})
    check("same state + new id -> merge_only", d3.action == "merge_only" and d3.merged_ids.get("imdb_id") == "tt1160419")


if __name__ == "__main__":
    for t in [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]:
        t()
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)
