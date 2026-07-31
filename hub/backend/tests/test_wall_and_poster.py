"""Unit tests for watching.py's live-wall broadcaster and poster cache.

Both replaced designs whose cost scaled with something we don't control — the
number of people watching the wall, and the size of the images visitors happen
to request. These pin the new behaviour.

Run:  python hub/backend/tests/test_wall_and_poster.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projects import watching as W  # noqa: E402

results = []
def check(name, cond):
    results.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


# ── live-wall broadcaster ───────────────────────────────────────────────────

async def _subscribe_and_take(frames, poll_counter):
    """Mimic what the /stream handler does: register a queue, make sure the
    shared poller is running, then drain `frames` snapshots."""
    key = next(W._wall_seq)
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    W._wall_subscribers[key] = q
    if W._wall_poller is None or W._wall_poller.done():
        W._wall_poller = asyncio.create_task(W._wall_poll_loop())
    got = 0
    try:
        for _ in range(frames):
            await asyncio.wait_for(q.get(), timeout=3)
            got += 1
    finally:
        W._wall_subscribers.pop(key, None)
    return got


async def test_one_poll_serves_every_viewer():
    """The whole point: N viewers must NOT mean N Jellyfin polls. Before this,
    gen() was defined per-request so each viewer ran their own loop."""
    calls = {"n": 0}

    async def fake_now():
        calls["n"] += 1
        return {"ok": True, "sessions": [{"user": f"snap{calls['n']}"}]}

    real_now, real_interval = W.now, W.STREAM_INTERVAL
    W.now, W.STREAM_INTERVAL = fake_now, 0.05
    W._wall_last = None
    try:
        got = await asyncio.gather(*[_subscribe_and_take(3, calls) for _ in range(5)])
        check("all 5 viewers received their frames", all(g == 3 for g in got))
        # 5 viewers x 3 frames would be 15 polls under the old per-client design.
        check(f"one shared poll loop (made {calls['n']} polls, not ~15)", calls["n"] <= 6)
    finally:
        W.now, W.STREAM_INTERVAL = real_now, real_interval


async def test_poller_stops_when_idle():
    """An empty wall should generate zero Jellyfin traffic."""
    calls = {"n": 0}

    async def fake_now():
        calls["n"] += 1
        return {"ok": True, "sessions": []}

    real_now, real_interval = W.now, W.STREAM_INTERVAL
    W.now, W.STREAM_INTERVAL = fake_now, 0.05
    try:
        await _subscribe_and_take(2, calls)
        await asyncio.sleep(0.2)          # let the loop notice it has no subscribers
        settled = calls["n"]
        await asyncio.sleep(0.25)
        check("poller stops once the last viewer leaves", calls["n"] == settled)
        check("poller task is cleared for the next subscriber",
              W._wall_poller is None or W._wall_poller.done())
    finally:
        W.now, W.STREAM_INTERVAL = real_now, real_interval


async def test_bad_poll_does_not_kill_the_stream():
    """One Jellyfin hiccup must degrade to an ok:false frame, not tear down
    everyone's connection."""
    state = {"n": 0}

    async def flaky_now():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("jellyfin exploded")
        return {"ok": True, "sessions": []}

    real_now, real_interval = W.now, W.STREAM_INTERVAL
    W.now, W.STREAM_INTERVAL = flaky_now, 0.05
    W._wall_last = None
    try:
        got = await _subscribe_and_take(2, state)
        check("stream survives a failing poll and keeps delivering", got == 2)
    finally:
        W.now, W.STREAM_INTERVAL = real_now, real_interval


# ── poster cache ────────────────────────────────────────────────────────────

def test_poster_cache_is_byte_bounded():
    real_max = W._POSTER_CACHE_MAX_BYTES
    W._poster_cache.clear()
    W._poster_cache_bytes = 0
    W._POSTER_CACHE_MAX_BYTES = 1000
    try:
        W._poster_cache_put(("a", "", 360, "primary"), b"x" * 600, "image/jpeg")
        W._poster_cache_put(("b", "", 360, "primary"), b"x" * 600, "image/jpeg")
        check("oldest entry evicted to stay under the byte budget",
              list(W._poster_cache) == [("b", "", 360, "primary")])
        check("byte counter tracks contents", W._poster_cache_bytes == 600)

        W._poster_cache_put(("huge", "", 1600, "backdrop"), b"x" * 5000, "image/jpeg")
        check("an image bigger than the whole budget is skipped, not cached",
              ("huge", "", 1600, "backdrop") not in W._poster_cache)
        check("skipping an oversized image doesn't corrupt the counter",
              W._poster_cache_bytes == 600)

        # re-inserting the same key must not double-count
        W._poster_cache_put(("b", "", 360, "primary"), b"y" * 300, "image/jpeg")
        check("re-inserting a key replaces rather than double-counts",
              W._poster_cache_bytes == 300)
    finally:
        W._POSTER_CACHE_MAX_BYTES = real_max
        W._poster_cache.clear()
        W._poster_cache_bytes = 0


async def main():
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
