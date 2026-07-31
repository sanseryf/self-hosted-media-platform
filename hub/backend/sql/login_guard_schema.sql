-- Failed/successful login attempts, backing the brute-force guard in
-- projects/login_guard.py. Deliberately a table rather than an in-memory
-- counter: unlike watch-together's room registry (ephemeral, high-churn,
-- documented as in-memory on purpose), login attempts are low-volume and
-- security-relevant, so surviving a `docker compose up -d --build` matters
-- more than avoiding a write. It also makes "who has been hammering the
-- door" an ordinary SQL question.

CREATE TABLE IF NOT EXISTS hub_login_attempts (
    id           bigserial PRIMARY KEY,
    ip           text        NOT NULL,
    username     text        NOT NULL,
    succeeded    boolean     NOT NULL DEFAULT false,
    attempted_at timestamptz NOT NULL DEFAULT now()
);

-- The guard only ever asks "how many FAILURES for this key since T" — partial
-- indexes on succeeded=false keep these small and skip the successful rows
-- entirely, which are kept only for audit.
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_recent
    ON hub_login_attempts (ip, attempted_at DESC) WHERE NOT succeeded;

CREATE INDEX IF NOT EXISTS idx_login_attempts_user_recent
    ON hub_login_attempts (lower(username), attempted_at DESC) WHERE NOT succeeded;

-- Supports the retention sweep (prune_old rows by age).
CREATE INDEX IF NOT EXISTS idx_login_attempts_attempted_at
    ON hub_login_attempts (attempted_at);
