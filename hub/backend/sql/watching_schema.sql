-- =====================================================================
-- Media Hub — Watch analytics: schema + function
-- =====================================================================
-- "Now watching" is read live from Jellyfin /Sessions (no storage needed).
-- "Watch time by genre" is accumulated over time by watch_poll.py, which
-- polls /Sessions on a cron and drops one heartbeat per actively-playing
-- session. minutes = poll interval, so SUM(minutes) ~= real watch time.
--
--   jf_items         cached item metadata (Movie/Episode, runtime)
--   jf_item_genres   item -> genre (many-to-many)
--   watch_heartbeats one row per poll per playing session
--   fn_watch_by_genre(kind) -> minutes + plays per genre
-- =====================================================================

CREATE TABLE IF NOT EXISTS jf_items (
    item_id      TEXT PRIMARY KEY,
    name         TEXT,
    media_type   TEXT,               -- 'Movie' or 'Episode'
    series_name  TEXT,               -- for episodes
    runtime_min  NUMERIC,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jf_item_genres (
    item_id  TEXT NOT NULL,
    genre    TEXT NOT NULL,
    PRIMARY KEY (item_id, genre)
);

CREATE TABLE IF NOT EXISTS watch_heartbeats (
    id          BIGSERIAL PRIMARY KEY,
    seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id  TEXT NOT NULL,       -- Jellyfin session + item, dedups a play
    item_id     TEXT NOT NULL,
    user_name   TEXT,
    minutes     NUMERIC NOT NULL     -- poll interval attributed to this beat
);

CREATE INDEX IF NOT EXISTS ix_heartbeats_item ON watch_heartbeats (item_id);
CREATE INDEX IF NOT EXISTS ix_heartbeats_time ON watch_heartbeats (seen_at);

-- ---------------------------------------------------------------------
-- Watch time by genre. A title with N genres attributes its minutes to
-- each of those genres (standard for genre breakdowns). kind filters to
-- 'movies', 'tv', or 'all'.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_watch_by_genre(kind TEXT DEFAULT 'all')
RETURNS TABLE (
    genre      TEXT,
    media_kind TEXT,
    minutes    NUMERIC,
    plays      BIGINT
) LANGUAGE sql STABLE AS $$
    SELECT
        g.genre,
        CASE WHEN i.media_type = 'Movie' THEN 'Movies' ELSE 'TV' END AS media_kind,
        SUM(h.minutes)                       AS minutes,
        COUNT(DISTINCT h.session_id)         AS plays
    FROM watch_heartbeats h
    JOIN jf_items       i ON i.item_id = h.item_id
    JOIN jf_item_genres g ON g.item_id = h.item_id
    WHERE kind = 'all'
       OR (kind = 'movies' AND i.media_type =  'Movie')
       OR (kind = 'tv'     AND i.media_type <> 'Movie')
    GROUP BY g.genre, media_kind
    ORDER BY minutes DESC;
$$;
