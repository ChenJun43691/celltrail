-- backend/app/db/migration_share_links.sql
-- ============================================================
-- 分享連結（12 小時臨時免登入檢視）
-- 執行：
--   docker exec -i celltrail_db psql -U celltrail -d celltrail < backend/app/db/migration_share_links.sql
-- 或：
--   psql "$DATABASE_URL" -f backend/app/db/migration_share_links.sql
--
-- 設計考量：
--   - 安全模型：「任何人持連結即可免登入檢視」。連結唯一防線是 token 的
--     不可猜測性 → token 用 secrets.token_urlsafe(24)（≈192 bits 熵），
--     暴力枚舉在實務上不可行。
--   - 純檢視：此表只授予「看某 project 地圖」的權限，不涉及任何寫入，
--     檢視者也拿不到下載報告 / 匯出的入口。
--   - 12 小時：expires_at 由後端在建立時一律設為 created_at + 12h，
--     不開放呼叫端自訂，避免有人開出永久連結。
--   - revoked_at：owner 可在到期前手動撤銷（連結外流時的補救手段）。
--     連結是否有效 = revoked_at IS NULL AND expires_at > now()。
--   - created_by 可為 NULL：AUTH_ENABLED=false 的匿名 admin（id=0）不在
--     users 表，FK 必須塞 NULL（與 project_members.granted_by 同理）。
--   - use_count / last_used_at：供 owner 觀察連結被開過幾次。每次開啟
--     另外會寫一筆 audit_logs（action=share_link.view，含 IP）。
-- ============================================================
CREATE TABLE IF NOT EXISTS share_links (
    id           BIGSERIAL   PRIMARY KEY,
    token        TEXT        NOT NULL UNIQUE,
    project_id   TEXT        NOT NULL,
    created_by   BIGINT      NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ NULL,
    last_used_at TIMESTAMPTZ NULL,
    use_count    INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_share_links_token   ON share_links (token);
CREATE INDEX IF NOT EXISTS idx_share_links_project ON share_links (project_id);
