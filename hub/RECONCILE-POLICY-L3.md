# L3b — Reconciler + Policy Auto-Approver

Two independent backend services on the L1/L2 substrate. **No UI here** — the admin
views (findings browser, decision log) are deferred to the devops-console refresh;
these expose gated JSON APIs.

## Reconciler — "does everything agree?"

Three-way drift detection between Jellyfin, the *arr apps, and disk (proxied by *arr
`hasFile`, since the hub container doesn't mount the library).

| File | Role |
|---|---|
| `backend/reconcile_diff.py` | pure set-diff (matches by any shared id, media-type-guarded). Unit-tested |
| `backend/sql/reconcile_schema.sql` | `reconcile_runs` + `reconcile_findings` |
| `backend/projects/reconcile.py` | enumerate via gateway → diff → store; gated endpoints |

Findings: **orphan_in_jellyfin** (JF has it, no *arr record), **missing_from_jellyfin**
(*arr has the file, JF never indexed it), **arr_wanted** (monitored, no file — informational).

```
POST /api/reconcile/run        # enumerate + diff + store (heavy; trigger/cron it, no auto-loop)
GET  /api/reconcile/last
GET  /api/reconcile/findings?run_id=&kind=&limit=
```

**Key error point:** if *any* source fails to enumerate, the run is stored `ok=false`
and **no diff is produced** — an empty list from a failed Jellyfin call would otherwise
report the entire *arr library as "missing from Jellyfin". Never diff partial data.

## Policy Auto-Approver — SHADOW MODE by default

Rules-based decisioning on Jellyseerr pending requests, with a full audit trail.

| File | Role |
|---|---|
| `backend/policy_rules.py` | pure `evaluate(ctx, rules) -> approve/manual/deny` + reasons. Unit-tested |
| `backend/sql/policy_schema.sql` | `policy_decisions` (audit log, one per event — idempotent) |
| `backend/projects/policy.py` | context-gather (best-effort) + decide + record + guarded enforce |

```
POST /api/policy/evaluate-pending   # decide un-decided pending requests
GET  /api/policy/decisions          # the audit log
GET  /api/policy/config             # active rules + enforce flag
```

**Two safety flags, both OFF by default (deploy-time opt-in):**
- `POLICY_ENFORCE=false` — decisions are computed and logged but **never acted on**.
  Shadow mode lets you watch what it *would* approve before it touches anything.
- **No background loop** — evaluation runs only when the endpoint is called, so it makes
  no outbound Jellyseerr calls on its own.

Rules via env: `POLICY_MAX_PENDING` (10), `POLICY_MIN_DISK_GB` (50), `POLICY_DENY_TYPES`,
`POLICY_ALLOW_4K` (false). Enforcement uses a **non-retrying** POST (writes aren't
safe to blind-retry). ⚠️ **Verify the approve endpoint** (`/request/{id}/approve`)
against your Jellyseerr version before enabling `POLICY_ENFORCE`.

Default posture is conservative: a request auto-approves only if it clears every gate;
otherwise it's routed to a human (manual), never silently denied.

## Verify

```bash
python hub/backend/tests/test_reconcile_policy.py     # 11 pure checks, no DB / no servers
```
