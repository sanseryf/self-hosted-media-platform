"""Pure-logic tests for the reconciler diff and the policy evaluator (no DB).

Run:  python hub/backend/tests/test_reconcile_policy.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reconcile_diff as R  # noqa: E402
import policy_rules as P  # noqa: E402

results = []
def check(name, cond):
    results.append(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


# ── reconciler ────────────────────────────────────────────────────────────────
def test_reconcile_matches_by_any_id():
    jf = [
        {"title": "Starfall Protocol", "media_type": "movie", "tmdb_id": "438631", "imdb_id": None, "tvdb_id": None},
        {"title": "Ghost Movie", "media_type": "movie", "tmdb_id": "111", "imdb_id": None, "tvdb_id": None},
        {"title": "The Long Meridian", "media_type": "series", "tmdb_id": None, "imdb_id": None, "tvdb_id": "368211"},
    ]
    arr = [
        # Starfall Protocol matches jf via tmdb even though arr also knows imdb
        {"title": "Starfall Protocol", "media_type": "movie", "tmdb_id": "438631", "imdb_id": "tt1160419",
         "tvdb_id": None, "has_file": True, "monitored": True},
        # a downloaded movie Jellyfin never indexed
        {"title": "Hollow Creek", "media_type": "movie", "tmdb_id": "1233413", "imdb_id": None,
         "tvdb_id": None, "has_file": True, "monitored": True},
        # The Long Meridian matches via tvdb; monitored but no file yet
        {"title": "The Long Meridian", "media_type": "series", "tmdb_id": None, "imdb_id": None,
         "tvdb_id": "368211", "has_file": False, "monitored": True},
    ]
    out = R.diff(jf, arr)
    orphans = {o["title"] for o in out["orphan_in_jellyfin"]}
    missing = {m["title"] for m in out["missing_from_jellyfin"]}
    wanted = {w["title"] for w in out["arr_wanted"]}
    check("Ghost Movie is orphaned in Jellyfin", orphans == {"Ghost Movie"})
    check("Hollow Creek missing from Jellyfin (has file, not indexed)", missing == {"Hollow Creek"})
    check("The Long Meridian is arr_wanted (monitored, no file)", wanted == {"The Long Meridian"})
    check("Starfall Protocol matched both ways (no finding)",
          "Starfall Protocol" not in orphans and "Starfall Protocol" not in missing)


def test_reconcile_media_type_guards_id_collision():
    # movie tmdb 5 and series tmdb 5 must NOT match each other
    jf = [{"title": "M", "media_type": "movie", "tmdb_id": "5", "imdb_id": None, "tvdb_id": None}]
    arr = [{"title": "S", "media_type": "series", "tmdb_id": "5", "imdb_id": None,
            "tvdb_id": None, "has_file": True, "monitored": True}]
    out = R.diff(jf, arr)
    check("movie:5 vs series:5 do not cross-match",
          len(out["orphan_in_jellyfin"]) == 1 and len(out["missing_from_jellyfin"]) == 1)


# ── policy ────────────────────────────────────────────────────────────────────
def test_policy_approves_clean_request():
    d = P.evaluate({"media_type": "movie", "pending_count": 1, "disk_free_gb": 800, "is_4k": False},
                   P.Rules())
    check("clean movie request -> approve", d.action == "approve")


def test_policy_holds_on_gates():
    r = P.Rules(max_pending_per_user=5, min_disk_free_gb=50)
    check("over quota -> manual",
          P.evaluate({"media_type": "movie", "pending_count": 5, "disk_free_gb": 800}, r).action == "manual")
    check("low disk -> manual",
          P.evaluate({"media_type": "movie", "pending_count": 0, "disk_free_gb": 10}, r).action == "manual")
    check("4k without allow_4k -> manual",
          P.evaluate({"media_type": "movie", "is_4k": True}, r).action == "manual")


def test_policy_denies_configured_type():
    r = P.Rules(deny_media_types=("tv",))
    d = P.evaluate({"media_type": "tv"}, r)
    check("denied media type -> deny with reason", d.action == "deny" and bool(d.reasons))
    # config says "tv", the event says "series" (normalized at ingest): same type
    check("deny 'tv' also denies a normalized 'series' event",
          P.evaluate({"media_type": "series"}, r).action == "deny")


def test_policy_auto_approves_series_by_default():
    d = P.evaluate({"media_type": "series", "pending_count": 0, "disk_free_gb": 800}, P.Rules())
    check("series clears the default type gate", d.action == "approve")


def test_policy_missing_context_skips_gate():
    # no disk / pending info available -> those gates are skipped, still approves a movie
    d = P.evaluate({"media_type": "movie"}, P.Rules())
    check("missing ctx skips gates, still approves", d.action == "approve")


if __name__ == "__main__":
    for t in [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]:
        t()
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)
