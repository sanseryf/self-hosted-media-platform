-- lifecycle_schema.sql — L3: per-title state machine + transition history.
--
-- `lifecycle` is one row per tracked title, holding the furthest-along state and
-- every external id we've learned for it (the identity-stitching store: a title
-- is matched across systems by ANY shared id). `lifecycle_transitions` is the
-- append-only timeline the L4 UI renders.
--
-- Applied idempotently at boot by lifecycle.ensure_schema().

CREATE TABLE IF NOT EXISTS lifecycle (
    id             bigserial PRIMARY KEY,
    media_type     text,                              -- movie | series
    tmdb_id        text,
    imdb_id        text,
    tvdb_id        text,
    title          text,
    state          text NOT NULL,                     -- current furthest state (see lifecycle_state.py)
    state_since    timestamptz NOT NULL DEFAULT now(),-- when it reached `state` (drives stall detection)
    requested_by   text,                              -- who asked for it (from Jellyseerr)
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Identity-stitch lookups: match an incoming event to an existing title by any id.
CREATE INDEX IF NOT EXISTS lifecycle_tmdb_idx  ON lifecycle (tmdb_id) WHERE tmdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lifecycle_imdb_idx  ON lifecycle (imdb_id) WHERE imdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lifecycle_tvdb_idx  ON lifecycle (tvdb_id) WHERE tvdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lifecycle_state_idx ON lifecycle (state);

CREATE TABLE IF NOT EXISTS lifecycle_transitions (
    id            bigserial PRIMARY KEY,
    lifecycle_id  bigint NOT NULL REFERENCES lifecycle(id) ON DELETE CASCADE,
    from_state    text,                               -- NULL on the first (create) transition
    to_state      text NOT NULL,
    source        text,                               -- which system's event caused it
    event_id      bigint,                             -- hub_events.id (loose coupling, no FK)
    event_type    text,
    at            timestamptz NOT NULL DEFAULT now()  -- upstream event time when known, else ingest time
);

CREATE INDEX IF NOT EXISTS lifecycle_trans_lid_idx ON lifecycle_transitions (lifecycle_id, at);
