"""Unit tests for auth.py's in-process session cache (pure, DB-free).

The cache is what turned a homepage load's 15+ session SELECTs into roughly
one, so the properties worth pinning down are the ones whose failure would be
either a security problem (a logged-out session still answering "valid") or a
silent memory leak.

Run:  python hub/backend/tests/test_session_cache.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projects import auth as A  # noqa: E402

results = []
def check(name, cond):
    results.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def _reset():
    A._session_cache.clear()


def test_miss_then_hit():
    _reset()
    hit, row = A._cache_get("sid-1")
    check("unknown id is a miss, not a cached-None", hit is False and row is None)
    A._cache_put("sid-1", {"jellyfin_username": "alice"})
    hit, row = A._cache_get("sid-1")
    check("after put, it hits", hit is True and row["jellyfin_username"] == "alice")


def test_negative_caching_is_distinguishable():
    """A cached "this session is invalid" must be a HIT carrying None, not a
    miss — otherwise every request with a junk cookie re-queries the DB, which
    is exactly the flood the negative TTL exists to stop."""
    _reset()
    A._cache_put("sid-bad", None)
    hit, row = A._cache_get("sid-bad")
    check("cached-invalid is a hit carrying None", hit is True and row is None)


def test_negative_ttl_is_shorter_than_positive():
    check("negative TTL < positive TTL", A._SESSION_NEG_TTL < A._SESSION_TTL)
    _reset()
    A._cache_put("pos", {"x": 1})
    A._cache_put("neg", None)
    pos_exp = A._session_cache["pos"][0]
    neg_exp = A._session_cache["neg"][0]
    check("invalid entries expire sooner than valid ones", neg_exp < pos_exp)


def test_expiry():
    _reset()
    A._cache_put("sid-exp", {"jellyfin_username": "bob"})
    # Rewrite the stored deadline into the past rather than sleeping.
    _exp, row = A._session_cache["sid-exp"]
    A._session_cache["sid-exp"] = (time.monotonic() - 1, row)
    hit, row = A._cache_get("sid-exp")
    check("expired entry reads as a miss", hit is False and row is None)
    check("expired entry is dropped on read", "sid-exp" not in A._session_cache)


def test_evict_is_immediate():
    """Logout's correctness depends entirely on this: the DB row is deleted,
    but nothing would stop the cache answering "still valid" for up to
    _SESSION_TTL seconds without an explicit evict."""
    _reset()
    A._cache_put("sid-out", {"jellyfin_username": "carol"})
    A._cache_evict("sid-out")
    hit, row = A._cache_get("sid-out")
    check("evicted session is gone at once", hit is False and row is None)
    A._cache_evict("never-existed")  # must not raise
    check("evicting an unknown id is a no-op", True)


def test_bounded_growth():
    """An unbounded dict keyed on attacker-suppliable cookie values is a memory
    leak with a trigger. Fill well past the cap with junk ids and confirm it
    stops growing."""
    _reset()
    for i in range(A._SESSION_CACHE_MAX + 500):
        A._cache_put(f"junk-{i}", None)
    check(f"cache stays <= {A._SESSION_CACHE_MAX} entries under flood",
          len(A._session_cache) <= A._SESSION_CACHE_MAX)


def test_eviction_prefers_already_expired():
    """When the cap is hit, dead entries should go before live ones — a live
    session shouldn't be evicted while stale junk is still resident."""
    _reset()
    A._cache_put("live", {"jellyfin_username": "dave"})
    for i in range(A._SESSION_CACHE_MAX):
        A._cache_put(f"dead-{i}", None)
        A._session_cache[f"dead-{i}"] = (time.monotonic() - 1, None)
    A._cache_put("newcomer", {"jellyfin_username": "erin"})
    hit, row = A._cache_get("live")
    check("live session survives an eviction sweep of dead entries",
          hit is True and row is not None)


if __name__ == "__main__":
    for t in [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]:
        t()
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)
