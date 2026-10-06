# Building a demo found two production bugs

**Type:** latent correctness bugs · **Area:** Hub event pipeline and approval
policy · **Found by:** the demo harness · **Status:** fixed with regression
tests

## Context

Hub can't be shown publicly without exposing a private server, so the repo
ships a [demo](../demo/). The real Hub image runs against mock upstreams, and
a seeder drives it through the **public webhook API** with realistic
payloads. Doing that end to end for the first time turned up two bugs that
unit tests had missed, because each unit was correct on its own.

## Bug 1: TV requests split into two titles

**Symptom:** the demo seeded 10 titles, and the lifecycle table showed 13.
Every TV series appeared twice: one row stuck at *requested*, and a second
row that progressed to *available* and *played*, with no requester.

**Cause:** each upstream names TV differently.

| Source | `media_type` as parsed from its webhook |
|---|---|
| Jellyseerr | `tv` |
| Sonarr | `series` |
| Jellyfin | `Series` |

The lifecycle tracker stitches events to a title by external IDs, guarded by
a matching `media_type` (so a movie and a series that share a numeric ID never
merge). That guard is correct, but it compared `tv` with `series` and decided
they were different titles.

**Fix:** normalize `media_type` once, at ingest, into a single vocabulary
(`tv`/`show` → `series`, `film` → `movie`). The approval policy got the same
canonicalization, so rules written as `tv` still match. A regression test now
feeds all three sources' spellings and asserts they agree.

## Bug 2: the approval audit log was always empty

**Symptom:** the policy engine reported `approve: 1`, but
`/api/policy/decisions` returned nothing. The server log showed:

```
policy decision store failed: there is no unique or exclusion constraint
matching the ON CONFLICT specification
```

**Cause:** the table's unique index is **partial**:

```sql
CREATE UNIQUE INDEX … ON policy_decisions (event_id) WHERE event_id IS NOT NULL;
```

Postgres only uses a partial index as the `ON CONFLICT` arbiter when the
statement repeats the index's predicate. The insert said
`ON CONFLICT (event_id) DO NOTHING` without it, so every insert raised. The
exception was caught and logged as a warning, so the run still reported
success.

**Impact:** no decision was ever recorded, and the "don't re-decide an event
that already has a decision" check never matched, so the same request was
re-evaluated on every run. The engine runs in shadow mode, so nothing was
wrongly approved. With enforcement on, it would have re-sent approvals.

**Fix:**

```sql
ON CONFLICT (event_id) WHERE event_id IS NOT NULL DO NOTHING
```

The statement now lives in one constant, and an integration test runs it
twice against real Postgres and asserts exactly one row. A mock couldn't have
caught this, because the bug is in Postgres's own arbiter inference.

## Lessons

- **Integration paths hide bugs that unit tests can't see.** Every function
  was right; the system wasn't.
- **A swallowed exception plus a success summary is a false all-clear.** The
  run said "approve: 1" while storing nothing.
- **Demos are tests.** The harness now runs in CI, so a change that breaks the
  demo fails the build.
