-- =====================================================================
-- Hub — auth: per-visitor Jellyfin login sessions
-- =====================================================================
-- Phase 2a (login only, no playback). An opaque, random session_id cookie
-- is handed to the browser; this table maps it server-side to the real
-- Jellyfin per-user token. The token itself never reaches the browser.
--
-- Unlike watching_schema.sql (applied lazily by the standalone watch_poll.py
-- cron), this table is on the critical path of the first login, so auth.py's
-- ensure_schema() applies it at hub boot, right after db.connect().
--
-- Every lookup filters `WHERE session_id=$1 AND expires_at > now()`, so
-- expired rows are simply invisible — no cleanup cron needed yet.
-- =====================================================================

CREATE TABLE IF NOT EXISTS hub_sessions (
    session_id          TEXT PRIMARY KEY,
    jellyfin_user_id    TEXT NOT NULL,
    jellyfin_username   TEXT NOT NULL,
    jellyfin_token      TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hub_sessions_expires ON hub_sessions (expires_at);
