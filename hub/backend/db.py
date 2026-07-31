"""Async Postgres access for the hub and its SQL projects.

A single asyncpg connection pool, created on startup and shared by every
project router. Connection is non-fatal: if the DB is down the hub still
serves the landing page, and /api/health reports the DB as unavailable.
"""
from __future__ import annotations

import os
from typing import Any

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_pool: asyncpg.Pool | None = None


async def connect() -> None:
    global _pool
    if not DATABASE_URL or _pool is not None:
        return
    try:
        # max_size was 5 — too small once the site-wide session gate
        # (app.py's require_session_for_api) made *every* /api/* request do
        # its own pool.acquire(): a single homepage load fires ~15+ gated
        # requests in parallel (rails, stats, posters, trending), each
        # needing a connection just to run has_valid_session(), on top of
        # whatever each handler's own query needs. Postgres has plenty of
        # headroom (max_connections=100, ~9 in use across every app on the
        # box at once) — there's no reason Hub's own pool should be the
        # bottleneck. See Media-Forest-MASTER-Handoff.md's P0 401 writeup.
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    except Exception:  # noqa: BLE001 — hub must boot even if the DB is down
        _pool = None


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool | None:
    """Project routers call this to run queries: `async with db.pool().acquire()`."""
    return _pool


async def health() -> dict[str, Any]:
    if not DATABASE_URL:
        return {"ok": False, "error": "DATABASE_URL not set"}
    if _pool is None:
        # try a lazy reconnect so a DB that came up later is picked up
        await connect()
    if _pool is None:
        return {"ok": False, "error": "no connection pool"}
    try:
        async with _pool.acquire() as conn:
            ver = await conn.fetchval("SELECT version()")
        return {"ok": True, "version": (ver or "").split(" on ")[0]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
