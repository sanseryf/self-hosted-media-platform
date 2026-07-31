-- policy_schema.sql — L3 policy auto-approver: an audit log of every decision.
-- Applied idempotently at boot by policy.ensure_schema().

CREATE TABLE IF NOT EXISTS policy_decisions (
    id             bigserial PRIMARY KEY,
    decided_at     timestamptz NOT NULL DEFAULT now(),
    event_id       bigint,                 -- the hub_events row that triggered it
    request_id     text,                   -- Jellyseerr request id (for enforcement)
    requested_by   text,
    media_type     text,
    tmdb_id        text,
    title          text,
    action         text NOT NULL,          -- approve | manual | deny
    reasons        text,                   -- why (human-readable, the audit trail)
    enforced       boolean NOT NULL DEFAULT false,   -- did we actually act (only when POLICY_ENFORCE)
    enforce_result text
);

-- One decision per source event → idempotent: re-processing the inbox never
-- double-decides (or double-approves) the same request.
CREATE UNIQUE INDEX IF NOT EXISTS policy_decisions_event_uniq
    ON policy_decisions (event_id) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS policy_decisions_decided_idx ON policy_decisions (decided_at DESC);
