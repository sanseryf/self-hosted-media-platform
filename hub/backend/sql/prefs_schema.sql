-- =====================================================================
-- Hub — per-user preferences
-- =====================================================================
-- Small key-of-preferences table, keyed by the Jellyfin *display username*
-- (same public-facing value hub_sessions and watch-together already join on,
-- never the Jellyfin user id/token). One row per person, created lazily the
-- first time they change a setting — absence of a row means "all defaults."
--
-- allow_hot_join: opt-in to watch-together's "Watch with {me}" hot-join. When
-- false (the default), a solo watcher is NOT offered up as joinable on anyone
-- else's Now Playing panel and the /hot-join auto-create path refuses to
-- promote them to a room host. This is the privacy default: nobody can drop
-- into your solo watch unless you've turned this on. See watching.py's now()
-- (sets each session's `joinable`) and together.py's hot_join() (server-side
-- re-check on the auto-create path).
--
-- Applied at boot by prefs.ensure_schema(), same as auth_schema.sql — cheap,
-- idempotent, and on no critical path other than the prefs endpoints
-- themselves (a missing table just degrades every preference to its default).
-- =====================================================================

CREATE TABLE IF NOT EXISTS hub_user_prefs (
    jellyfin_username   TEXT PRIMARY KEY,
    allow_hot_join      BOOLEAN NOT NULL DEFAULT false,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
