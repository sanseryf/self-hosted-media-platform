# Hub

An event-driven integration platform that sits on top of a self-hosted media
stack — Jellyfin, Sonarr, Radarr and Jellyseerr — and answers the questions none
of them can answer alone: *where is my request right now, do these four systems
still agree with each other, and should this request have been approved
automatically?*

It also happens to be the site members log into. FastAPI + asyncpg, one
container, no build step.

```
                 webhooks
 Jellyfin ─┐   (idempotent,
 Sonarr   ─┤    shared-secret)      ┌── L3  lifecycle tracker  (per-title state machine)
 Radarr   ─┼──────────────►  L1 ────┼── L3  reconciler         (three-way drift detection)
 Jellyseerr┘                inbox   └── L3  policy engine      (auto-approve, shadow by default)
                              │
                              │      L2  typed connector gateway
                              └──────────  one normalized contract over four APIs
                                                    │
                                                    └── L4  member surfaces
                                                        landing page · live wall ·
                                                        request journey · in-browser player
```

## Why this exists

Off-the-shelf tooling covers each box in isolation. Nothing correlates them. A
request that silently stalls between "Jellyseerr approved it" and "Jellyfin
indexed it" is invisible to all four apps — each one believes it did its job.
Hub is the layer that notices.

| Layer | What it does | Docs |
|---|---|---|
| **L1** Event ingestion | Idempotent, self-authenticating webhook receiver. Every event from four upstreams lands in one Postgres inbox, deduped and correlation-tagged, with a dead-letter path for anything unparseable. | [EVENTS-L1.md](EVENTS-L1.md) |
| **L2** Connector gateway | One typed client per upstream behind a normalized `MediaRef`/`SystemState` contract — timeouts, retry with jittered backoff, an explicit error taxonomy. L3 reads one shape instead of four bespoke clients. | [GATEWAY-L2.md](GATEWAY-L2.md) |
| **L3** Lifecycle tracker | Correlates events into a per-title state machine (`requested → grabbed → downloading → downloaded → available → played`, with `failed`/`deleted` side states). Handles out-of-order delivery and resolves identity across TMDB/IMDB/TVDB ids. | [LIFECYCLE-L3.md](LIFECYCLE-L3.md) |
| **L3b** Reconciler + policy | Three-way drift detection between Jellyfin, the *arr apps and disk; plus a Jellyseerr auto-approver that runs in **shadow mode unless `POLICY_ENFORCE=true`** — it logs the decision it *would* have made until you trust it. | [RECONCILE-POLICY-L3.md](RECONCILE-POLICY-L3.md) |
| **L4** Member surfaces | The payoff: landing page, live Now Playing wall (SSE), per-title request journey, in-browser playback with watch-together sync. | [SURFACES-L4.md](SURFACES-L4.md) |

## Design decisions worth arguing about

**The state machine is pure and separately tested.** `lifecycle_state.py` has no
database access at all — it's event-in, decision-out. The DB executor is a
different file. That split is why out-of-order delivery (a late `grabbed` arriving
after `available`) is a 25-check unit test that runs anywhere rather than
something you find out about in production.

**Shadow mode is the default, not a flag someone remembered to set.** The policy
engine writes every decision to a log and changes nothing until explicitly
enforced. An auto-approver you can't audit before trusting is one you shouldn't
run.

**The rate limiter fails closed; everything else fails open.** Hub's house rule is
"degrade, never 500" — a dead upstream returns `{"ok": false}` and the page still
renders. `login_guard.py` deliberately inverts that: a database error there
refuses the login. It costs nothing, because minting a session needs the same
database anyway — so failing closed can't deny a login that would have succeeded,
and it removes "knock the DB over, then brute force freely" as a strategy.

**Watch-together rooms are in-memory on purpose.** High-churn, fully ephemeral
state (a seek fires every few seconds) doesn't belong in the same Postgres the
nightly ETL shares. The trade — a redeploy drops open rooms — is accepted
explicitly and documented at the top of `together.py`, along with the condition
that would invalidate it (a second worker or replica).

**One session lookup per page, not fifteen.** Every `/api/*` route is behind a
session gate, and a real homepage load fires 15+ gated requests in parallel. A
short-TTL in-process cache collapses those into roughly one query; logout evicts
directly rather than waiting for the TTL.

## Security model

The landing page is a login gate. Hiding media in the frontend is not a control —
`curl /api/library/items` would walk straight past it — so the lock lives in
`app.py`'s middleware: **every** `/api/*` route requires a valid session. Exactly
three things are public, and each for a stated reason:

| Public surface | Why |
|---|---|
| `/api/auth/*` | You cannot log in through a gate that requires being logged in. Rate-limited per-username and per-IP (`login_guard.py`). |
| `/api/events/ingest/*` | The senders are containers, not people — they can't hold a session, so they authenticate with a shared secret instead. Only the *ingest* sub-prefix; the observability reads stay gated. |
| `/api/health` | Liveness probes. Answers `GET` and `HEAD`. |

Sessions are opaque random ids in an httponly cookie, mapped server-side to the
real Jellyfin per-user token. **That token never appears in a response body.**
The one deliberate exception is the video stream URL itself — `<video src>` can't
send an `Authorization` header, so the token rides in the URL exactly as
Jellyfin's own web client does it. The reasoning, and the alternative that was
rejected (proxying multi-GB range requests through FastAPI), is written out at
the top of `playback.py`.

Responses carry a CSP, `X-Content-Type-Options`, `Referrer-Policy`,
`Permissions-Policy` and `frame-ancestors 'none'`.

## Layout

```
hub/
├─ backend/
│  ├─ app.py                 middleware stack, router mounts, asset versioning
│  ├─ db.py                  asyncpg pool shared by every router
│  ├─ event_sources.py       pure per-source payload parsers        (L1)
│  ├─ lifecycle_state.py     pure state machine                     (L3)
│  ├─ reconcile_diff.py      pure three-way set diff                (L3b)
│  ├─ policy_rules.py        pure auto-approve rules                (L3b)
│  ├─ connectors/            typed clients + gateway facade         (L2)
│  ├─ projects/              one router per capability
│  │   ├─ auth.py            login, sessions, in-process cache
│  │   ├─ login_guard.py     per-username + per-IP brute-force guard
│  │   ├─ playback.py        in-browser playback, subtitle proxy
│  │   ├─ together.py        watch-together rooms + SSE sync
│  │   └─ …                  events, gateway, lifecycle, reconcile, policy, library, prefs
│  ├─ sql/                   one schema file per owning module
│  ├─ static/                index.html · app.css · app.js · tokens.css · wall · journey
│  └─ tests/                 9 suites, DB-free ones run anywhere
├─ Dockerfile
├─ docker-compose.yml
└─ requirements.txt
```

Each `sql/*.sql` file is owned by exactly one module, applied at boot by that
module's `ensure_schema()`. No migration tool: schemas are `CREATE TABLE IF NOT
EXISTS`, and a database that's down means Hub still boots and serves the page.

## Frontend

No framework, no bundler, no build step — but not unstructured:

- `tokens.css` is the **single** definition of the design system (six themes,
  spacing/radius/type scales). Every page links it. It used to be duplicated
  inline in `index.html`, the two copies drifted, and CI now fails if a second
  definition reappears.
- `app.css` and `app.js` are cached with `immutable` and a content-hash query
  string injected at serve time; `index.html` itself always revalidates. A deploy
  that only touches markup re-downloads 7 KB, not 260 KB.
- Everything is gzipped. Every animation honours `prefers-reduced-motion`, touch
  targets meet 44 px under `pointer: coarse`, and colour tokens clear WCAG AA
  contrast on every surface.

## Running it

```bash
docker network create datanet          # once, shared with the postgres stack
cp .env.example .env && $EDITOR .env   # DATABASE_URL, JELLYFIN_*, EVENTS_TOKEN
docker compose up -d --build
curl -s localhost:8090/api/health      # {"ok":true,"db":{"ok":true,...}}
```

Local development, no containers:

```bash
pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --reload --port 8090
```

Leave `JELLYFIN_API_KEY` and `HUB_JELLYFIN_LOGIN_ENABLED` unset locally. Both are
deliberate safety gates: without them, the routes that talk to Jellyfin
short-circuit before making any network call, so a dev machine on the same LAN
can't reach — or act on — the real server. `HUB_JELLYFIN_LOGIN_ENABLED` exists
specifically because per-user login is the one path that needs no admin key and
would otherwise reach production unaided.

## Tests

```bash
for f in backend/tests/test_*.py; do python "$f" || exit 1; done
```

Each file is a standalone script that exits non-zero on failure. Suites needing a
database skip cleanly when `DATABASE_URL` is unset, so the pure-logic tests — the
parsers, the state machine, the diff, the caches — run anywhere. CI runs the lot
against a real Postgres service container.

## Adding a capability

1. Create `backend/sql/<name>_schema.sql` if it needs tables.
2. Add `backend/projects/<name>.py` with an `APIRouter` and an
   `ensure_schema()`. Own your tables — read them from other modules through
   functions you export, not by querying them directly. (`auth.py` owns
   `hub_sessions`; nothing else touches it.)
3. Register the router and schema in `app.py`.
4. Put the pure logic in its own module at `backend/` root and unit-test it
   without a database.
