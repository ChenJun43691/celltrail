-- ============================================================
-- CellTrail Migration: 多角色權限系統
-- 執行：psql "$DATABASE_URL" -f backend/app/db/migration_permissions.sql
-- 冪等：多次執行安全（IF NOT EXISTS / IF NOT EXISTS）
-- ============================================================

-- ---------- 擴充 users 表 ----------
ALTER TABLE users ADD COLUMN IF NOT EXISTS real_name        TEXT        NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS unit             TEXT        NULL;  -- 單位（例：高市刑大）
ALTER TABLE users ADD COLUMN IF NOT EXISTS badge_number     TEXT        NULL;  -- 警號
ALTER TABLE users ADD COLUMN IF NOT EXISTS email            TEXT        NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active        BOOLEAN     NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;

-- is_active 索引：停用帳號查詢用
CREATE INDEX IF NOT EXISTS idx_users_active ON users (is_active) WHERE is_active = TRUE;

-- ---------- project_members 表 ----------
-- 紀錄哪位使用者對哪個 project 有什麼權限。
--
-- permission 三層：
--   owner        → 可授權他人、刪除目標、PATCH azimuth-ref；等同 admin 限於此案件
--   collaborator → 可上傳資料、查看軌跡
--   viewer       → 唯讀（map-layers、evidence-report）
--
-- expires_at NULL = 永久授權；非 NULL = 有效期到期後視為無效（查詢時以 now() 比對）
--
-- granted_by：授權者的 user_id；系統自動授權（首次上傳=owner）時可為 NULL
CREATE TABLE IF NOT EXISTS project_members (
    id          BIGSERIAL    PRIMARY KEY,
    project_id  TEXT         NOT NULL,
    user_id     BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission  TEXT         NOT NULL
                CHECK (permission IN ('owner', 'collaborator', 'viewer')),
    expires_at  TIMESTAMPTZ  NULL,
    granted_by  BIGINT       NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (project_id, user_id)   -- 一個 user 在同一個 project 只能有一個 permission
);

CREATE INDEX IF NOT EXISTS idx_project_members_project
    ON project_members (project_id);
CREATE INDEX IF NOT EXISTS idx_project_members_user
    ON project_members (user_id);
-- 有效授權查詢最佳化（expires_at IS NULL OR expires_at > now()）
CREATE INDEX IF NOT EXISTS idx_project_members_active
    ON project_members (project_id, user_id)
    WHERE expires_at IS NULL;
