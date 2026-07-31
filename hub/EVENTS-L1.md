# L1 — Event Ingestion (webhook receiver)

The foundation of the integration platform: an **idempotent, self-authenticating
webhook receiver** that lands every event from the four upstreams (Jellyfin,
Sonarr, Radarr, Jellyseerr) into one Postgres inbox (`hub_events`), deduped and
correlation-tagged, with a dead-letter path for anything it can't parse.

Everything above L1 (lifecycle tracker, reconciler, policy engine, live wall)
reads from this table instead of re-polling four APIs.

## Files

| File | What it is |
|---|---|
| `backend/sql/events_schema.sql` | `hub_events` inbox + `hub_events_dead` view + indexes |
| `backend/event_sources.py` | pure per-source payload → `Envelope` parsers (no DB, unit-tested) |
| `backend/projects/events.py` | the router: `POST /ingest/{source}` + gated `recent`/`dead`/`stats` |
| `backend/tests/test_event_sources.py` | 8 extractor unit tests (run anywhere) |

Wiring in `app.py`: router mounted at `/api/events`, schema applied at boot, and
`/api/events/ingest/` added to the session-gate exemption list (only the ingest
sub-prefix — the reads stay gated).

## Config

Add to `hub`'s production `.env` (the schema auto-applies on next boot):

```
EVENTS_TOKEN=<generate a long random secret>     # shared secret the webhooks present
```

`DATABASE_URL` is already set. **If `EVENTS_TOKEN` is unset the ingest endpoint is
open** (fine for local dev, logged as a warning) — set it in production, since
`/ingest` is deliberately outside Hub's session gate.

Generate one:
```bash
openssl rand -hex 24
```

## Endpoints

```
POST /api/events/ingest/{source}   source ∈ jellyfin|sonarr|radarr|jellyseerr
                                    auth: header  X-Hub-Token: <EVENTS_TOKEN>
                                          or query ?token=<EVENTS_TOKEN>
GET  /api/events/recent[?source=&limit=]   latest events           (session-gated)
GET  /api/events/dead[?limit=]             dead-letter queue        (session-gated)
GET  /api/events/stats                     24h counts + dead total  (session-gated)
```

Response contract on ingest:
- `200 {ok:true, deduped:false, id}` — stored
- `200 {ok:true, deduped:true}` — duplicate delivery, no-op (idempotent)
- `202 {ok:false, dead_letter:true, error}` — unparseable/unknown, stored to DLQ, **don't retry**
- `401` — bad/missing token · `404` — unknown source · `503` — DB down, **please retry**

## Wire the four webhooks

Point each sender at the hub on the LAN (`http://10.0.0.10:8090`, or the
docker-network name `http://hub:8090` if the sender shares hub's network).

**Sonarr / Radarr** → Settings → Connect → **+ → Webhook**
- URL: `http://10.0.0.10:8090/api/events/ingest/sonarr` (or `/radarr`)
- Method: `POST`
- Triggers: On Grab, On Import/Download, On Upgrade, On Movie/Series Delete, On File Delete
- Add header `X-Hub-Token` = your `EVENTS_TOKEN` (or append `?token=...` to the URL)

**Jellyfin** → Dashboard → Plugins → **Webhook** (install if absent) → Add Generic Destination
- Webhook URL: `http://10.0.0.10:8090/api/events/ingest/jellyfin`
- Notification types: Playback Start/Stop/Progress, Item Added, Authentication Success/Failure, etc.
- Add a request header `X-Hub-Token` = `EVENTS_TOKEN`, and `Content-Type: application/json`

**Jellyseerr** → Settings → Notifications → **Webhook**
- Webhook URL: `http://10.0.0.10:8090/api/events/ingest/jellyseerr`
- Authorization Header: your `EVENTS_TOKEN` — **or** append `?token=...` to the URL
  (Jellyseerr's auth header isn't named `X-Hub-Token`, so the query-param form is simplest here)
- JSON payload: keep the **default template** — the parser reads `notification_type`,
  `subject`, `media.{tmdbId,tvdbId,media_type}`, `request.{request_id,requestedBy_username}`
- Notification types: Request Pending, Approved, Available, Failed

## Verify

```bash
# from the box (replace TOKEN):
curl -s -XPOST "http://10.0.0.10:8090/api/events/ingest/radarr?token=TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"eventType":"Test","movie":{"title":"Smoke","tmdbId":1,"imdbId":"tt0"}}'
# -> {"ok":true,"deduped":false,"id":...}   (repeat the same call -> "deduped":true)
```

Then (logged into the hub) hit `GET /api/events/recent` and `GET /api/events/stats`.

## Tests

```bash
python hub/backend/tests/test_event_sources.py     # 8 extractor unit tests, no DB needed
```

## How L1 feeds the rest

`hub_events` carries `imdb_id` / `tmdb_id` / `tvdb_id` on every row — the join
keys L3's lifecycle tracker uses to stitch one title's `MEDIA_APPROVED` (Jellyseerr)
→ `Grab`/`Download` (arr) → `Item Added` / `PlaybackStart` (Jellyfin) into a single
state machine. L1 just has to land them durably and deduped; correlation is L3.
