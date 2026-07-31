# L2 — Typed Connector Gateway

One typed, resilient client per upstream (Jellyfin, Sonarr, Radarr, Jellyseerr)
behind a **normalized contract** (`MediaRef` / `SystemState`), so L3 reads one
shape instead of four bespoke `urllib` clients: one adapter per upstream, one
contract above them, and resilience (timeouts, retry, typed errors) made
explicit rather than repeated in every caller.

## Files (`hub/backend/connectors/`)

| File | Role |
|---|---|
| `base.py` | `HttpConnector`: timeouts, retry+backoff+jitter, the typed error taxonomy, the "disabled" guard |
| `models.py` | normalized `MediaRef`, `SystemState`, `HealthStatus`, `as_id()` |
| `jellyfin.py` `radarr.py` `sonarr.py` `jellyseerr.py` | the four connectors |
| `gateway.py` | `Gateway` facade — concurrent `health()` + `locate()` fan-out |
| `__init__.py` | env factory + `init/get/close_gateway()` singleton (mirrors `db.py`) |

Router: `projects/gateway.py` → `GET /api/gateway/health`, `GET /api/gateway/locate`
(session-gated). Wired in `app.py`'s lifespan (`init_gateway` / `close_gateway`).

## Error points (handled explicitly — the whole point of L2)

| # | Failure mode | How it's handled |
|---|---|---|
| 1 | **Upstream hangs** | connect + read timeouts (`httpx.Timeout(15, connect=5)`) so one dead app can't wedge the event loop |
| 2 | **Transient blips** (conn reset, read timeout, 5xx) | bounded retry (2) with exp backoff + full jitter — GET-only, so blind retry is safe |
| 3 | **4xx won't-improve** (400/422) | raised immediately, never retried |
| 4 | **Bad/missing key** (401/403) | `ConnectorAuthError`, no retry — distinct from "down" |
| 5 | **Not found** (404) | `ConnectorNotFound`, which `find_*()` maps to `None` |
| 6 | **2xx but junk body** (HTML error page) | `ConnectorBadResponse`, not a silent `{}` |
| 7 | **Dev box hits prod by accident** | URL defaults point at prod, so a connector with **no API key is disabled** — zero network calls, `health='not configured'` |
| 8 | **id type drift** (tmdbId is int in arr/seerr, str in Jellyfin) | `as_id()` coerces every id to `str` for correlation |
| 9 | **Jellyfin provider-query casing** | `AnyProviderIdEquals=tmdb.<id>` must be **lowercased** though `ProviderIds` come back `Tmdb`/`Imdb`/`Tvdb`; wrong casing silently returns 0 hits |
| 10 | **Auth is per-app** | Jellyfin `X-Emby-Token`, *arr/Jellyseerr `X-Api-Key`; in headers not query strings (keeps keys out of access logs) |
| 11 | **API base differs** | Jellyfin has no `/api/vN`; *arr use `/api/v3`; Jellyseerr `/api/v1` |
| 12 | **"absent" ≠ error** | Radarr/Sonarr `?tmdbId=`/`?tvdbId=` return `[]` for not-in-library, and Jellyseerr returns `mediaInfo:null` — all mapped to `SystemState.absent`, not treated as failures |
| 13 | **Sonarr keys on TVDB, not TMDB** | correlation from a Sonarr record must join via `tvdb_id`; L3 must carry all three ids |
| 14 | **Jellyseerr needs the media type** | `/movie/{id}` vs `/tv/{id}` — no `media_type` → return `None`, don't guess wrong endpoint |
| 15 | **One slow upstream sinking a batch** | `gateway.locate()` isolates each branch; a connector error becomes `None` for that source only |

**Known not-yet-handled (deliberate, for later):** no circuit breaker (retry only —
fine for LAN upstreams; add if an upstream flaps); retry path is GET-only, so L3's
policy-engine POSTs must not reuse it blindly (non-idempotent).

## Config

Reuses the env the hub already uses — `JELLYFIN_URL`/`JELLYFIN_API_KEY` (or `JELLYFIN`),
and `RADARR_*`, `SONARR_*`, `JELLYSEERR_*`, plus `SRV` for the host default. Any
connector whose key is unset runs disabled. New deps added to `requirements.txt`:
`httpx>=0.27`, `pydantic>=2.7`.

## Verify

```bash
python hub/backend/tests/test_connectors.py     # 25 checks, no DB / no live servers
```

Once real keys are in the hub `.env`, logged in:
```
GET /api/gateway/health                          # per-connector up/down + latency
GET /api/gateway/locate?tmdb=438631&media_type=movie
# -> {"views": {"jellyfin": {...state: available}, "radarr": {...}, "jellyseerr": {...}}}
```

## How L2 feeds L3

`gateway.locate(ids)` returns each system's `MediaRef` for one title. L3's lifecycle
tracker takes an L1 event (which carries the correlation ids), calls `locate()` to
enrich it with current cross-system state, and advances the per-title state machine.
