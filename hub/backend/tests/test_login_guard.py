"""Integration tests for the /api/auth/login brute-force guard.

Needs a real Postgres because the guard's whole behaviour lives in one
windowed, indexed COUNT — mocking that would only test the mock. Set
DATABASE_URL and it runs; without one it SKIPS (exit 0) rather than failing, so
a machine with no database doesn't turn into a red build.

Run:  DATABASE_URL=postgres://... python hub/backend/tests/test_login_guard.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from projects import login_guard as G  # noqa: E402

results = []
def check(name, cond):
    results.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


PREFIX = "guardtest_"


async def _clear():
    p = db.pool()
    async with p.acquire() as c:
        await c.execute("DELETE FROM hub_login_attempts WHERE username LIKE $1", PREFIX + "%")


async def test_clean_slate_is_allowed():
    await _clear()
    check("a first attempt is never throttled",
          await G.check("203.0.113.1", PREFIX + "alice") is None)


async def test_username_budget():
    await _clear()
    ip, user = "203.0.113.2", PREFIX + "bob"
    for i in range(G.MAX_FAILURES_PER_USER):
        check(f"attempt {i + 1} still allowed", await G.check(ip, user) is None)
        await G.record(ip, user, succeeded=False)
    wait = await G.check(ip, user)
    check(f"throttled after {G.MAX_FAILURES_PER_USER} failures", wait is not None and wait > 0)
    check("retry-after is inside the window", wait <= G.WINDOW_MINUTES * 60)


async def test_username_lock_does_not_spill():
    """One member fumbling their password must not lock out the household."""
    await _clear()
    ip = "203.0.113.3"
    for _ in range(G.MAX_FAILURES_PER_USER):
        await G.record(ip, PREFIX + "carol", succeeded=False)
    check("locked user is throttled", await G.check(ip, PREFIX + "carol") is not None)
    check("a different user on the same IP is unaffected",
          await G.check(ip, PREFIX + "dave") is None)


async def test_username_lock_follows_the_name_across_ips():
    """Otherwise a botnet just rotates source IPs against one account."""
    await _clear()
    for i in range(G.MAX_FAILURES_PER_USER):
        await G.record(f"203.0.113.{100 + i}", PREFIX + "erin", succeeded=False)
    check("still throttled from a brand-new IP",
          await G.check("198.51.100.55", PREFIX + "erin") is not None)


async def test_ip_budget_trips_across_many_usernames():
    """And otherwise one host just rotates usernames instead."""
    await _clear()
    ip = "203.0.113.200"
    for i in range(G.MAX_FAILURES_PER_IP):
        await G.record(ip, f"{PREFIX}user{i}", succeeded=False)
    check("per-IP budget trips on spread-out attempts",
          await G.check(ip, PREFIX + "never_seen") is not None)


async def test_success_clears_the_slate():
    await _clear()
    ip, user = "203.0.113.4", PREFIX + "frank"
    for _ in range(G.MAX_FAILURES_PER_USER - 1):
        await G.record(ip, user, succeeded=False)
    await G.record(ip, user, succeeded=True)
    check("a correct password wipes earlier failures",
          await G.check(ip, user) is None)


async def test_case_insensitive_username():
    """Jellyfin usernames aren't case-sensitive to a human typing them; the
    guard shouldn't hand out a fresh budget for 'Alice' vs 'alice'."""
    await _clear()
    ip = "203.0.113.5"
    for _ in range(G.MAX_FAILURES_PER_USER):
        await G.record(ip, PREFIX + "grace", succeeded=False)
    check("differently-cased username is still throttled",
          await G.check(ip, (PREFIX + "grace").upper()) is not None)


async def test_fails_closed_without_a_database():
    """Deliberate inversion of Hub's usual degrade-gracefully rule. Costs
    nothing: login needs the same DB to store a session, so a DB-down login was
    going to fail anyway — and it removes "DoS the database, then brute force"."""
    real_pool = db.pool
    db.pool = lambda: None
    try:
        wait = await G.check("203.0.113.6", PREFIX + "heidi")
        check("no database -> refuse, don't wave through", wait is not None)
    finally:
        db.pool = real_pool


def test_client_ip_takes_the_last_forwarded_entry():
    """nginx APPENDS the real peer to any client-sent X-Forwarded-For, so the
    last entry is the only one a client can't forge. Reading the first would
    let anyone choose (and rotate) their own rate-limit bucket."""
    class FakeReq:
        def __init__(self, headers, peer="10.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": peer})()

    check("spoofed leading entry is ignored",
          G.client_ip(FakeReq({"x-forwarded-for": "1.2.3.4, 198.51.100.7"})) == "198.51.100.7")
    check("single-entry XFF is used as-is",
          G.client_ip(FakeReq({"x-forwarded-for": "198.51.100.7"})) == "198.51.100.7")
    check("no XFF falls back to the socket peer",
          G.client_ip(FakeReq({}, peer="10.9.9.9")) == "10.9.9.9")


async def main():
    if not os.environ.get("DATABASE_URL"):
        print("  SKIP  no DATABASE_URL set — login guard integration tests skipped")
        test_client_ip_takes_the_last_forwarded_entry()   # pure, runs anywhere
        print(f"\n{sum(results)}/{len(results)} checks passed.")
        return 0 if all(results) else 1

    await db.connect()
    if db.pool() is None:
        print("  SKIP  DATABASE_URL set but unreachable — skipping")
        return 0
    await G.ensure_schema()

    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()

    await _clear()
    await db.close()
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
