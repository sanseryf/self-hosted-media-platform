-- events_schema.sql — L1 of the integration platform: the idempotent event inbox.
--
-- One table (`hub_events`) is the durable landing zone for every webhook the four
-- upstreams push at us (Jellyfin, Sonarr, Radarr, Jellyseerr). It doubles as:
--   • the idempotency ledger — a UNIQUE(dedup_key) turns at-least-once delivery
--     into effectively-once storage (ingest does ON CONFLICT DO NOTHING);
--   • the dead-letter queue — rows we couldn't parse/recognize land here with
--     status='dead' and the error, so nothing is ever silently dropped;
--   • the correlation store L3 joins on — the extracted imdb/tmdb/tvdb ids are
--     what let one title be followed across all four systems.
--
-- Applied idempotently at boot by events.ensure_schema() (see projects/events.py),
-- exactly like auth_schema.sql — every statement is IF NOT EXISTS / OR REPLACE so
-- re-running it on an already-migrated DB is a no-op.

CREATE TABLE IF NOT EXISTS hub_events (
    id            bigserial PRIMARY KEY,
    source        text NOT NULL,                     -- jellyfin | sonarr | radarr | jellyseerr
    event_type    text NOT NULL,                     -- upstream event name (NotificationType / eventType / notification_type)
    dedup_key     text NOT NULL,                     -- idempotency key: provided id if any, else a hash of the canonical event tuple
    status        text NOT NULL DEFAULT 'received',  -- received | processed | dead
    attempts      int  NOT NULL DEFAULT 0,           -- processing attempts (L3 increments; L1 sets 1 on dead-letter)
    last_error    text,                              -- why it dead-lettered / last processing failure

    -- Cross-system correlation keys. At least one of these is what L3's lifecycle
    -- tracker joins on; a single title emits events into all four systems and they
    -- only line up by shared external id.
    imdb_id       text,
    tmdb_id       text,
    tvdb_id       text,
    title         text,
    media_type    text,                              -- movie | series | episode | ...
    user_name     text,                              -- who triggered it, where the payload says (playback/request events)

    occurred_at   timestamptz,                       -- upstream's own event time, if it sent one (else NULL — use received_at)
    received_at   timestamptz NOT NULL DEFAULT now(),
    processed_at  timestamptz,                        -- set when L3 consumes it (NULL = still in the inbox)
    payload       jsonb NOT NULL,                     -- the raw webhook body, kept verbatim for replay/debugging

    CONSTRAINT hub_events_dedup_key_uniq UNIQUE (dedup_key)
);

-- Observability / dashboard reads (recent feed, per-source-per-type counts).
CREATE INDEX IF NOT EXISTS hub_events_received_idx    ON hub_events (received_at DESC);
CREATE INDEX IF NOT EXISTS hub_events_source_type_idx ON hub_events (source, event_type);
-- Partial index on the "needs attention" rows (dead / unprocessed) — keeps the
-- status board and any future L3 drain query cheap without indexing the huge,
-- uninteresting mass of already-processed events.
CREATE INDEX IF NOT EXISTS hub_events_open_idx        ON hub_events (status) WHERE status <> 'processed';

-- Correlation lookups for L3's join-across-systems. Partial so we only index rows
-- that actually carry each id (an arr movie event has tmdb+imdb but no tvdb, etc.).
CREATE INDEX IF NOT EXISTS hub_events_tmdb_idx ON hub_events (tmdb_id) WHERE tmdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS hub_events_imdb_idx ON hub_events (imdb_id) WHERE imdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS hub_events_tvdb_idx ON hub_events (tvdb_id) WHERE tvdb_id IS NOT NULL;

-- Dead-letter as a view over the inbox rather than a second table: explicit and
-- queryable for the status page, with no row-moving on failure (the row is born
-- dead in place). L1 only ever produces 'dead'; L3 will later be what flips rows
-- to 'processed' or back to 'dead' with a last_error.
CREATE OR REPLACE VIEW hub_events_dead AS
    SELECT * FROM hub_events WHERE status = 'dead';
