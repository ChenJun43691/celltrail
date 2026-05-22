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

-- 帳號自訂密碼：使用者申請時自設密碼（bcrypt hash），admin 核准後直接以此 hash 建帳號。
-- 改版前送出的申請此欄為 NULL，approve 端點偵測到 NULL 會退回「產生臨時密碼」舊流程。
ALTER TABLE account_requests ADD COLUMN IF NOT EXISTS password_hash TEXT;
