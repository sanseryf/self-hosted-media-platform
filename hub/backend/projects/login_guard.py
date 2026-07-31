"""Brute-force guard for POST /api/auth/login.

    check(ip, username)    -> None if allowed, or seconds-to-wait if throttled
    record(ip, username, succeeded)
    prune_loop()           -> background retention sweep (started in app.py)

Why this exists: /api/auth/login is one of only three route prefixes outside
the site-wide session gate (see app.py's _PUBLIC_API_PREFIXES) — it has to be,
since it's how you get a session in the first place. That makes it the one
internet-reachable endpoint that forwards a username/password to Jellyfin's
/Users/AuthenticateByName, with nothing between an attacker and unlimited
credential stuffing. This module is that "something between."

── Two independent windows, not one ────────────────────────────────────────
Counting only per-IP lets a botnet spread one password across many hosts;
counting only per-username lets one host enumerate many accounts. So both are
checked and the stricter verdict wins:

  * per-username  5 failures / 15 min  — tight, because a real person fumbling
                    their own password rarely exceeds this, and the blast
                    radius of a lockout is one account.
  * per-IP       20 failures / 15 min  — deliberately looser: a household or
                    campus behind one NAT is a single IP to us, and locking
                    that out would take down every legitimate member on it.

Both are *soft* windows — nothing is stored as "locked until X". The guard
re-counts failures inside the trailing window on every attempt, so a throttle
decays on its own as old failures age out; there is no lock record to leak,
expire, or have to clear by hand.

── Failing closed is free here ─────────────────────────────────────────────
Every other read path in Hub fails open-ish (degrade, never 500). This one
fails CLOSED — a DB error while counting means the request is refused. That
costs nothing, because auth.py cannot mint a session without the same database
anyway: if the DB is down, login was going to fail two steps later regardless.
So there is no scenario where failing closed denies a login that would
otherwise have succeeded, and it removes "knock the DB over, then brute force
freely" as a strategy.

── A successful login clears the slate ─────────────────────────────────────
record(..., succeeded=True) deletes that username's outstanding failures, so
someone who mistypes four times and then gets it right isn't left one slip
away from a lockout for the rest of the window.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import Request

import db

log = logging.getLogger("hub.login_guard")

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "login_guard_schema.sql"

# Trailing window both counters look back over.
WINDOW_MINUTES = 15
# Failure budget inside that window, per key. See the module docstring for why
# these two numbers are deliberately far apart.
MAX_FAILURES_PER_USER = 5
MAX_FAILURES_PER_IP = 20
# How long attempt rows are kept before the sweep drops them. Longer than the
# window on purpose: the extra days are for answering "was I being probed last
# Tuesday", not for rate limiting.
RETENTION_DAYS = 7
_PRUNE_INTERVAL_SECS = 6 * 3600


async def ensure_schema() -> None:
    """Apply login_guard_schema.sql at boot. Non-fatal, exactly like
    auth.ensure_schema() — a DB that's down must not stop Hub from booting.
    Note this does NOT weaken the guard: check() fails closed, so a missing
    table means login is refused, not waved through."""
    p = db.pool()
    if p is None:
        return
    try:
        async with p.acquire() as conn:
            await conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.warning("login_guard: schema apply failed; check() will fail closed")


def client_ip(request: Request) -> str:
    """The caller's real IP, as seen from behind Nginx Proxy Manager.

    Takes the LAST entry of X-Forwarded-For, not the first. NPM (nginx) sets
    the header with `$proxy_add_x_forwarded_for`, which *appends* the immediate
    peer to whatever the client already sent — so a client that forges
    `X-Forwarded-For: 1.2.3.4` produces `1.2.3.4, <their real ip>`. Reading the
    first entry would let anyone pick their own rate-limit bucket (and rotate
    it per request); the last entry is the one nginx itself wrote and is the
    only one a client cannot influence. Falls back to the socket peer when the
    header is absent (direct/local access, no proxy in front).
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return (request.client.host if request.client else "") or "unknown"


async def check(ip: str, username: str) -> int | None:
    """Is this attempt allowed? Returns None to proceed, or the number of
    seconds the caller should wait (for a Retry-After header) if either the
    per-username or per-IP budget is spent.

    Fails CLOSED: a missing table or DB error returns a wait rather than None.
    See the module docstring for why that costs nothing here."""
    p = db.pool()
    if p is None:
        return WINDOW_MINUTES * 60
    username = (username or "").strip()
    try:
        async with p.acquire(timeout=3) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  count(*) FILTER (WHERE lower(username) = lower($2)) AS user_fails,
                  count(*) FILTER (WHERE ip = $1)                     AS ip_fails,
                  -- Oldest failure still inside the window for whichever key is
                  -- over budget; the caller waits until it ages out.
                  min(attempted_at) FILTER (WHERE lower(username) = lower($2)) AS user_oldest,
                  min(attempted_at) FILTER (WHERE ip = $1)                     AS ip_oldest
                FROM hub_login_attempts
                WHERE NOT succeeded
                  AND attempted_at > now() - make_interval(mins => $3)
                  AND (ip = $1 OR lower(username) = lower($2))
                """,
                ip, username, WINDOW_MINUTES,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("login_guard.check failed closed: %r", e)
        return WINDOW_MINUTES * 60

    if row is None:
        return None

    over_user = (row["user_fails"] or 0) >= MAX_FAILURES_PER_USER
    over_ip = (row["ip_fails"] or 0) >= MAX_FAILURES_PER_IP
    if not (over_user or over_ip):
        return None

    # Wait until the oldest failure in the offending bucket leaves the window.
    oldest = row["user_oldest"] if over_user else row["ip_oldest"]
    if oldest is None:
        return WINDOW_MINUTES * 60
    try:
        from datetime import datetime, timedelta, timezone
        expires = oldest + timedelta(minutes=WINDOW_MINUTES)
        remaining = int((expires - datetime.now(timezone.utc)).total_seconds())
        return max(1, min(remaining, WINDOW_MINUTES * 60))
    except Exception:  # noqa: BLE001
        return WINDOW_MINUTES * 60


async def record(ip: str, username: str, succeeded: bool) -> None:
    """Log one attempt. Best-effort and never raises — a guard that 500s the
    login route it protects would be worse than the problem. On success, also
    clears that username's outstanding failures (see module docstring)."""
    p = db.pool()
    if p is None:
        return
    username = (username or "").strip()
    try:
        async with p.acquire(timeout=3) as conn:
            await conn.execute(
                "INSERT INTO hub_login_attempts (ip, username, succeeded) VALUES ($1,$2,$3)",
                ip, username, bool(succeeded),
            )
            if succeeded:
                await conn.execute(
                    "DELETE FROM hub_login_attempts "
                    "WHERE NOT succeeded AND lower(username) = lower($1)",
                    username,
                )
    except Exception as e:  # noqa: BLE001
        log.warning("login_guard.record failed: %r", e)


async def prune_loop() -> None:
    """Retention sweep, started as a background task in app.py's lifespan
    alongside the status sampler and the room sweeper. Drops attempt rows past
    RETENTION_DAYS so the table stays small on its own."""
    while True:
        p = db.pool()
        if p is not None:
            try:
                async with p.acquire(timeout=5) as conn:
                    await conn.execute(
                        "DELETE FROM hub_login_attempts "
                        "WHERE attempted_at < now() - make_interval(days => $1)",
                        RETENTION_DAYS,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("login_guard.prune_loop: %r", e)
        await asyncio.sleep(_PRUNE_INTERVAL_SECS)
