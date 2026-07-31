-- =====================================================================
-- Media Hub — File Audit: schema + SQL functions
-- =====================================================================
-- Loaded by audit_collect.py on every run (idempotent), and safe to
-- paste into Adminer to explore. Raw SQL on purpose: window functions,
-- CTEs, array_agg, GROUP BY — the good stuff for the SQL portfolio.
--
--   fs_entries     one row per directory found under /data/media
--   audit_runs     one row per collector run (log)
--   fn_duplicate_titles()  -> groups of same-named titles (report only)
--   fn_empty_folders()     -> dirs with zero files anywhere below (purged)
--   fn_audit_summary()     -> per-type rollup for the dashboard
-- =====================================================================

CREATE TABLE IF NOT EXISTS fs_entries (
    id            BIGSERIAL PRIMARY KEY,
    scanned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    path          TEXT   NOT NULL,          -- relative to /data/media
    name          TEXT   NOT NULL,          -- basename as it sits on disk
    name_norm     TEXT   NOT NULL,          -- lowercased/stripped for dup match
    media_type    TEXT,                     -- Movies/TV/Anime/... or NULL
    depth         INT    NOT NULL,          -- levels below the media root
    is_title      BOOLEAN NOT NULL,         -- sits at its type's "title" depth
    file_count    INT    NOT NULL,          -- files directly in this dir
    subtree_files INT    NOT NULL,          -- files anywhere at/below this dir
    subtree_bytes BIGINT NOT NULL,          -- total bytes at/below this dir
    child_dirs    INT    NOT NULL           -- immediate sub-directories
);

CREATE INDEX IF NOT EXISTS ix_fs_entries_title ON fs_entries (media_type, name_norm) WHERE is_title;
CREATE INDEX IF NOT EXISTS ix_fs_entries_empty ON fs_entries (subtree_files);

CREATE TABLE IF NOT EXISTS audit_runs (
    id            BIGSERIAL PRIMARY KEY,
    ran_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    dirs_scanned  INT    NOT NULL,
    titles        INT    NOT NULL,
    dup_groups    INT    NOT NULL,
    empties_found INT    NOT NULL,
    empties_purged INT   NOT NULL,
    wasted_bytes  BIGINT NOT NULL
);

-- ---------------------------------------------------------------------
-- Duplicate titles: same media_type + normalized name appearing >1 time.
-- ROW_NUMBER picks a "keeper" (largest copy); the rest are wasted space.
-- Report only — never auto-deleted.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_duplicate_titles()
RETURNS TABLE (
    media_type   TEXT,
    name_norm    TEXT,
    copies       INT,
    keeper_path  TEXT,
    paths        TEXT[],
    total_bytes  BIGINT,
    wasted_bytes BIGINT
) LANGUAGE sql STABLE AS $$
    WITH ranked AS (
        SELECT
            e.media_type,
            e.name_norm,
            e.path,
            e.subtree_bytes,
            ROW_NUMBER() OVER (PARTITION BY e.media_type, e.name_norm
                               ORDER BY e.subtree_bytes DESC, e.path) AS rn,
            COUNT(*)     OVER (PARTITION BY e.media_type, e.name_norm) AS cnt,
            SUM(e.subtree_bytes) OVER (PARTITION BY e.media_type, e.name_norm) AS grp_bytes,
            MAX(e.subtree_bytes) OVER (PARTITION BY e.media_type, e.name_norm) AS keep_bytes
        FROM fs_entries e
        WHERE e.is_title
    )
    SELECT
        media_type,
        name_norm,
        cnt::int                                   AS copies,
        (array_agg(path ORDER BY rn))[1]           AS keeper_path,
        array_agg(path ORDER BY rn)                AS paths,
        grp_bytes                                  AS total_bytes,
        (grp_bytes - keep_bytes)                   AS wasted_bytes
    FROM ranked
    WHERE cnt > 1
    GROUP BY media_type, name_norm, cnt, grp_bytes, keep_bytes
    ORDER BY (grp_bytes - keep_bytes) DESC, copies DESC;
$$;

-- ---------------------------------------------------------------------
-- Empty folders: directories with zero files anywhere at/below them.
-- (A dir whose whole subtree is other empty dirs still counts as empty.)
-- Deepest first so the collector can rmdir bottom-up.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_empty_folders()
RETURNS TABLE (
    path       TEXT,
    media_type TEXT,
    child_dirs INT,
    depth      INT
) LANGUAGE sql STABLE AS $$
    SELECT e.path, e.media_type, e.child_dirs, e.depth
    FROM fs_entries e
    WHERE e.subtree_files = 0
    ORDER BY e.depth DESC, e.path;
$$;

-- ---------------------------------------------------------------------
-- Per-type rollup for the dashboard / console: title count, duplicate
-- groups, wasted bytes from dupes, and empty-dir count.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_audit_summary()
RETURNS TABLE (
    media_type       TEXT,
    titles           BIGINT,
    dup_groups       BIGINT,
    dup_wasted_bytes BIGINT,
    empty_dirs       BIGINT
) LANGUAGE sql STABLE AS $$
    WITH titles AS (
        SELECT media_type, COUNT(*) AS titles
        FROM fs_entries WHERE is_title
        GROUP BY media_type
    ),
    dupes AS (
        SELECT media_type,
               COUNT(*)          AS dup_groups,
               SUM(wasted_bytes) AS dup_wasted_bytes
        FROM fn_duplicate_titles()
        GROUP BY media_type
    ),
    empties AS (
        SELECT media_type, COUNT(*) AS empty_dirs
        FROM fs_entries WHERE subtree_files = 0
        GROUP BY media_type
    )
    SELECT
        COALESCE(t.media_type, d.media_type, e.media_type) AS media_type,
        COALESCE(t.titles, 0)                              AS titles,
        COALESCE(d.dup_groups, 0)                          AS dup_groups,
        COALESCE(d.dup_wasted_bytes, 0)                    AS dup_wasted_bytes,
        COALESCE(e.empty_dirs, 0)                          AS empty_dirs
    FROM titles t
    FULL JOIN dupes   d ON d.media_type = t.media_type
    FULL JOIN empties e ON e.media_type = COALESCE(t.media_type, d.media_type)
    ORDER BY media_type;
$$;

-- ---------------------------------------------------------------------
-- Largest titles: the biggest movie/show folders on disk (subtree bytes).
-- Powers the console "Largest titles" glance chart. Read-only.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_largest_titles(lim INT DEFAULT 20)
RETURNS TABLE (media_type TEXT, name TEXT, path TEXT, bytes BIGINT)
LANGUAGE sql STABLE AS $$
    SELECT media_type, name, path, subtree_bytes
    FROM fs_entries
    WHERE is_title
    ORDER BY subtree_bytes DESC
    LIMIT lim;
$$;
