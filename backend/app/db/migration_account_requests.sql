-- migration: account_requests table
CREATE TABLE IF NOT EXISTS account_requests (
    id          BIGSERIAL PRIMARY KEY,
    username    TEXT NOT NULL,
    real_name   TEXT NOT NULL,
    unit        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    reason      TEXT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ NULL,
    reviewed_by BIGINT NULL REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_account_requests_status ON account_requests(status);
CREATE INDEX IF NOT EXISTS idx_account_requests_phone  ON account_requests(phone);
