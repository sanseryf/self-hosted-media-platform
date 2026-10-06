"""Integration test: policy decisions are stored exactly once per event.

Needs a real Postgres because the behaviour under test is Postgres's own
ON CONFLICT arbiter inference against a PARTIAL unique index — the bug this
guards was invisible to any mock. Set DATABASE_URL and it runs; without one it
SKIPS (exit 0), same convention as the other integration tests.

Run:  DATABASE_URL=postgres://... python hub/backend/tests/test_policy_store.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from projects import policy  # noqa: E402

results = []
def check(name, cond):
    results.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


EVENT_ID = 990_000_001   # outside any real id range; cleaned up before and after


async def _count(conn):
    return await conn.fetchval("SELECT count(*) FROM policy_decisions WHERE event_id=$1", EVENT_ID)


async def main():
    await db.connect()
    await policy.ensure_schema()
    async with db.pool().acquire() as conn:
        await conn.execute("DELETE FROM policy_decisions WHERE event_id=$1", EVENT_ID)
        args = (EVENT_ID, "77", "alice", "movie", "9000001", "Starfall Protocol",
                "approve", "clears all gates", False, None)
        try:
            await conn.execute(policy.INSERT_DECISION_SQL, *args)
            check("first decision for an event is stored", await _count(conn) == 1)
            await conn.execute(policy.INSERT_DECISION_SQL, *args)
            check("re-processing the same event does not double-store", await _count(conn) == 1)
        except Exception as e:  # noqa: BLE001
            check(f"insert must not raise ({e!r})", False)
        finally:
            await conn.execute("DELETE FROM policy_decisions WHERE event_id=$1", EVENT_ID)
    await db.close()


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        print("SKIP: no DATABASE_URL set (this is the deploy/staging-time integration test).")
        sys.exit(0)
    asyncio.run(main())
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)
