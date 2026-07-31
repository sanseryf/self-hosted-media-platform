-- reconcile_schema.sql — L3 three-way reconciler: run history + findings.
-- Applied idempotently at boot by reconcile.ensure_schema().

CREATE TABLE IF NOT EXISTS reconcile_runs (
    id                    bigserial PRIMARY KEY,
    ran_at                timestamptz NOT NULL DEFAULT now(),
    ok                    boolean NOT NULL DEFAULT true,   -- false = a source failed to enumerate
    note                  text,                            -- why a run is not ok / partial
    jellyfin_items        int,
    arr_items             int,
    orphan_in_jellyfin    int,
    missing_from_jellyfin int,
    arr_wanted            int
);

CREATE TABLE IF NOT EXISTS reconcile_findings (
    id          bigserial PRIMARY KEY,
    run_id      bigint NOT NULL REFERENCES reconcile_runs(id) ON DELETE CASCADE,
    kind        text NOT NULL,   -- orphan_in_jellyfin | missing_from_jellyfin | arr_wanted
    title       text,
    media_type  text,
    tmdb_id     text,
    imdb_id     text,
    tvdb_id     text
);

CREATE INDEX IF NOT EXISTS reconcile_findings_run_idx ON reconcile_findings (run_id, kind);
