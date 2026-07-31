# L3 — Lifecycle Tracker (the flagship)

Correlates the events L1 collects into a **per-title state machine**, following each
title across all four systems from request to first play, and surfacing where any
title is stalled. This is the piece with no good OSS equivalent and the strongest
integration-engineer signal (correlation IDs, state machines, out-of-order handling,
identity resolution).

```
requested → grabbed → downloading → downloaded → available → played
                              (side: failed, deleted — apply from anywhere)
```

## Files

| File | Role |
|---|---|
| `backend/lifecycle_state.py` | **pure** state machine: event→state map, `decide()`, `plan()`. Unit-tested (25 checks) |
| `backend/sql/lifecycle_schema.sql` | `lifecycle` (one row/title, all known ids) + `lifecycle_transitions` (timeline) |
| `backend/projects/lifecycle.py` | DB executor (`process_pending`), background drain loop, read API |
| `backend/tests/test_lifecycle_state.py` | pure-logic tests (no DB) |
| `backend/tests/test_lifecycle_integration.py` | end-to-end, **runs when `DATABASE_URL` is set** |

Wired in `app.py`: schema at boot, `processor_loop()` started in the lifespan (drains
every 30s), router at `/api/lifecycle`.

## How it works

L1 lands events cheaply (`status='received'`). L3's drain loop pulls un-processed rows,
and for each one:
1. **enrich** (optional, off by default) — `gateway.locate()` backfills missing ids from live upstreams;
2. **stitch** — find the existing title matching *any* of the event's ids (of a compatible type);
3. **plan** — `lifecycle_state.plan()` decides create / advance / backfill-ids / nothing;
4. **execute** — one transaction: update the title + append a transition + mark the event processed.

## Error points (handled)

| # | Failure mode | Handling |
|---|---|---|
| 1 | **Out-of-order delivery** (retried "Grab" lands after "Available") | state only moves forward by rank; a lower-ranked event is a no-op — never regresses |
| 2 | **Different primary keys per system** (Jellyseerr=TMDB, Sonarr=TVDB, Jellyfin=both) | identity stitch: match a title by ANY shared id, and backfill newly-learned ids onto the row |
| 3 | **Cross-type TMDB collision** (movie 123 ≠ tv 123) | match is guarded by `media_type` when both are known |
| 4 | **One event bridges two existing rows** | logged as a known limitation; takes the earliest row (full row-merge is a documented TODO) |
| 5 | **Poison event** | each event drains in its own transaction; after `MAX_ATTEMPTS` (5) failures it's flipped to `dead` — this is what finally exercises L1's DLQ |
| 6 | **Ingest/processing coupling** | fully decoupled — L1 acks fast; L3 drains on its own loop, so a slow drain never slows a webhook |
| 7 | **Enrichment stalling the drain** | `locate()` enrichment is best-effort, wrapped, and OFF by default (`LIFECYCLE_ENRICH=true` to enable) — a dead upstream can't block processing |
| 8 | **Null event time** | transition/`state_since` uses `occurred_at` when present, else `received_at` — never null |
| 9 | **Re-entry after deletion** | `deleted`/`failed` are side states; a later forward event re-enters the ladder and records it |

## Read API

```
GET  /api/lifecycle/titles[?state=&limit=]   tracked titles + current state + stall flag
GET  /api/lifecycle/title/{id}               one title + full transition timeline
GET  /api/lifecycle/stalled                  titles past their per-state stall threshold
GET  /api/lifecycle/stats                    counts by state
POST /api/lifecycle/drain[?limit=]           run the processor now (session-gated; for demos)
```

Stall thresholds (`STALL_HOURS`): requested 48h, grabbed 24h, downloading 24h, downloaded 6h.
(available/played/failed/deleted are never "stalled".)

## Verify

```bash
python hub/backend/tests/test_lifecycle_state.py                 # 25 pure checks, no DB
DATABASE_URL=postgres://… python hub/backend/tests/test_lifecycle_integration.py
#   ^ simulates an out-of-order, cross-keyed 4-event sequence; asserts it stitches to
#     ONE title, ends 'played', and the timeline recorded the advances. Skips w/o a DB.
```

## Feeds L4

`lifecycle_transitions` is the timeline the L4 UI renders; `/stalled` is the status-page
"needs attention" list; `/stats` drives the state funnel.
