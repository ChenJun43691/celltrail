-- ============================================================
-- CellTrail Database Schema
-- ------------------------------------------------------------
-- 執行方式：
--   psql "$DATABASE_URL" -f backend/app/db/schema.sql
-- ============================================================

-- ---------- Extensions ----------
-- PostGIS：用於地理資料（geometry、ST_AsGeoJSON、ST_MakePoint 等）
CREATE EXTENSION IF NOT EXISTS postgis;

-- pgcrypto：用於 security.py 的 DB 端密碼驗證（crypt() 函式）
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- Users ----------
-- 注意：password_hash 由應用層以 bcrypt/pbkdf2_sha256 產生；
--       舊資料可由 pgcrypto 的 crypt() 相容驗證。
CREATE TABLE IF NOT EXISTS users (
    id             BIGSERIAL PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'user'
                   CHECK (role IN ('admin', 'user')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);

-- ---------- Raw Traces ----------
-- 每一列代表一筆基地台連線紀錄（經清洗與定位後的結果）
CREATE TABLE IF NOT EXISTS raw_traces (
    id           BIGSERIAL PRIMARY KEY,
    project_id   TEXT NOT NULL,
    target_id    TEXT NOT NULL,

    -- 時間（含時區）
    start_ts     TIMESTAMPTZ,
    end_ts       TIMESTAMPTZ,

    -- 基地台識別
    cell_id      TEXT,
    cell_addr    TEXT,
    sector_name  TEXT,
    site_code    TEXT,
    sector_id    TEXT,
    azimuth      INT,

    -- ── 方位角基準（P2.5 法庭可防禦性） ──
    -- 為什麼要這欄：
    --   電信業者交付 raw_traces 時，azimuth 欄位的「北方基準」並沒有統一規格。
    --   有些業者交付的是「磁北 magnetic north」、有些是「真北 true north」、
    --   有些根本沒寫清楚。台灣高雄區磁偏角約 -4°~-5°（西偏）；500 公尺距離下
    --   差出約 50 公尺，足以差出整條街。法庭被詢問「此方位角的北方基準為何」
    --   答不出來，整套基地台扇形覆蓋推論的採信度會被連帶質疑。
    --
    -- 設計：預設 'unknown'（不擅自推論）。要由偵查員查證電信業者書面交付規格後，
    -- 透過 PATCH /api/projects/{p}/targets/{t}/azimuth-ref 端點批次標註，
    -- 並由 audit_logs 記錄誰、何時、依何書面證據做的標註。
    azimuth_ref  TEXT NOT NULL DEFAULT 'unknown'
                 CHECK (azimuth_ref IN ('magnetic', 'true', 'unknown')),

    -- 座標（原始值，方便除錯；geom 由應用層或 trigger 產生）
    lat          DOUBLE PRECISION,
    lng          DOUBLE PRECISION,
    accuracy_m   INT,

    -- PostGIS 幾何欄位（WGS84）
    geom         geometry(Point, 4326),

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ── 軟刪欄位（P1 證物保全） ──
    -- 為什麼軟刪？刑事證物一旦進入系統，物理刪除等同事證滅失，被告律師
    -- 可主張「警方藏匿不利證據」。軟刪保留紀錄但隱藏，可隨時還原並由
    -- audit_logs 追溯誰在何時刪除、為何刪除（payload 內含理由）。
    deleted_at   TIMESTAMPTZ NULL,
    deleted_by   BIGINT NULL,
    delete_reason TEXT NULL
);

-- 對既有 DB 增量補欄位（不會重建表；既有資料一律設 deleted_at = NULL）
ALTER TABLE raw_traces ADD COLUMN IF NOT EXISTS deleted_at    TIMESTAMPTZ NULL;
ALTER TABLE raw_traces ADD COLUMN IF NOT EXISTS deleted_by    BIGINT      NULL;
ALTER TABLE raw_traces ADD COLUMN IF NOT EXISTS delete_reason TEXT        NULL;

-- 方位角基準（P2.5）：對既有資料補預設 'unknown'，新資料由 ingest 預設填入
ALTER TABLE raw_traces ADD COLUMN IF NOT EXISTS azimuth_ref TEXT NOT NULL DEFAULT 'unknown';
-- CHECK 約束在新建表已有；對既有 DB 補上同名約束（IF NOT EXISTS 不適用 CHECK，
-- 改用 DO 區塊偵測；多次重跑安全）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'raw_traces_azimuth_ref_check'
    ) THEN
        ALTER TABLE raw_traces
              ADD CONSTRAINT raw_traces_azimuth_ref_check
              CHECK (azimuth_ref IN ('magnetic', 'true', 'unknown'));
    END IF;
END$$;

-- ---------- Indexes ----------
CREATE INDEX IF NOT EXISTS idx_raw_traces_project_target
    ON raw_traces (project_id, target_id);
CREATE INDEX IF NOT EXISTS idx_raw_traces_start_ts
    ON raw_traces (start_ts);
CREATE INDEX IF NOT EXISTS idx_raw_traces_geom
    ON raw_traces USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_raw_traces_cell_id
    ON raw_traces (cell_id);

-- partial index：99% 的查詢都帶 deleted_at IS NULL，partial index 更小且更快
CREATE INDEX IF NOT EXISTS idx_raw_traces_active
    ON raw_traces (project_id, target_id)
    WHERE deleted_at IS NULL;

-- ---------- Audit Logs（P0 法庭可防禦性核心） ----------
-- 為什麼要這張表：
--   1. 證據鏈完整性（chain of custody）：誰在何時對哪筆證物做了什麼
--   2. 法庭採信要件（《刑事訴訟法》§159-4 特信性文書，要求製作過程具規律性）
--   3. 內控稽核：辨識誤刪、未授權存取、批次匿名異動
--
-- 設計原則：
--   - 永遠 INSERT，永不 UPDATE/DELETE（append-only ledger）
--   - 即使來源紀錄被刪除（軟刪），audit log 仍保留 target 識別字串
--   - details JSONB 保留彈性（不同 action 寫入不同欄位）
--   - payload_hash 為 SHA-256，方便事後驗證 details 未被竄改
CREATE TABLE IF NOT EXISTS audit_logs (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 操作者（AUTH_ENABLED=false 時 user_id=0, username='anonymous'）
    user_id       BIGINT      NULL,
    username      TEXT        NULL,
    role          TEXT        NULL,

    -- 動作別：upload | delete | restore | update | login | login_fail | export ...
    action        TEXT NOT NULL,

    -- 標的：raw_traces | target | project | user | session
    target_type   TEXT        NULL,
    target_ref    TEXT        NULL,   -- target_id / project_id 等業務鍵（字串保存）
    project_id    TEXT        NULL,

    -- 來源端
    ip            TEXT        NULL,
    user_agent    TEXT        NULL,

    -- 細節（彈性欄位）
    details       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    payload_hash  TEXT        NULL,    -- SHA-256(details::text)

    -- 結果
    status_code   INT         NULL,    -- HTTP-like：200 / 400 / 500
    error_text    TEXT        NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_ts
    ON audit_logs (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user
    ON audit_logs (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action
    ON audit_logs (action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_project
    ON audit_logs (project_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_details_gin
    ON audit_logs USING GIN (details);

-- ---------- Evidence Files（P2 證物全 hash 封存） ----------
-- 每筆上傳的原始檔案在進入 ingest 之前，都先把 raw bytes 的 SHA-256 落地。
-- 為什麼要拉一張單獨的表，而不只是把 hash 塞進 audit_logs.details：
--   1. audit_logs 的 details 是彈性 JSON，無法強制 unique 約束
--   2. 證物指紋是「物」，audit 記錄是「事」；分表符合資料模型語意
--   3. 一份檔案可能被軟刪後重新上傳（同一 sha256_full）→ 比對發現相同檔案
--      可協助識別「同一物證被誤刪後重新進系統」、「不同案件出現同一檔案」等情形
CREATE TABLE IF NOT EXISTS evidence_files (
    id            BIGSERIAL PRIMARY KEY,
    project_id    TEXT NOT NULL,
    target_id     TEXT NOT NULL,

    filename      TEXT NOT NULL,
    ext           TEXT NULL,        -- csv / xlsx / pdf ...
    size_bytes    BIGINT NOT NULL,
    sha256_full   TEXT NOT NULL,    -- 全檔案 SHA-256（hex 64 字元）
    mime_hint     TEXT NULL,        -- UploadFile.content_type（client 自報，僅供參考）

    uploaded_by   BIGINT NULL,
    uploaded_by_name TEXT NULL,
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 該次匯入結果統計（讓報告可一目了然）
    rows_total    INT NULL,
    rows_inserted INT NULL,
    rows_skipped  INT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_files_project_target
    ON evidence_files (project_id, target_id);
CREATE INDEX IF NOT EXISTS idx_evidence_files_sha256
    ON evidence_files (sha256_full);
CREATE INDEX IF NOT EXISTS idx_evidence_files_uploaded_at
    ON evidence_files (uploaded_at DESC);

-- ---------- Carrier Profiles（W1：欄位對照表從 code 搬到 DB） ----------
-- 為什麼要這張表：
--   過往 backend/app/services/ingest.py 裡的 _RAW2CANON 常數寫死在 code，
--   每次新業者出現新表頭都要：工程師改 code → review → deploy。
--   把這份對照表搬到 DB 後：
--     1. 「格式知識」與「處理邏輯」解耦 — 承辦人/管理員可以直接擴充對照表
--     2. 每筆 profile 都有 created_by / approved_by / approved_at 稽核欄位
--     3. 為 W2 模板指紋匹配與 W3 LLM 輔助建檔鋪路（llm_assisted / llm_model 欄位先建好）
--
-- 資料模型語意：
--   - 一筆 profile = 一個業者的一份格式 mapping（business_carrier × form_variant）
--   - is_default = TRUE 的那一筆：當 fingerprint 都沒命中時的全域 fallback
--   - mapping_json：{"原始欄名": "canonical 欄名", ...}（已正規化的 key 在執行期重算）
--   - fingerprint：W2 才會用，現在先留空
CREATE TABLE IF NOT EXISTS carrier_profiles (
    id              BIGSERIAL PRIMARY KEY,

    -- 識別
    carrier_name    TEXT NULL,                -- 中華電信 / 台灣大哥大 / 遠傳 / 亞太 / ... NULL=未分類
    variant_label   TEXT NOT NULL,            -- 例：'ChunghwaTelecom-2024Q3-form'、'default'
    fingerprint     TEXT NULL,                -- sha256(headers_sorted_joined)；W2 啟用

    -- 對照表本體
    mapping_json    JSONB NOT NULL,           -- {"開始連線時間":"start_ts", ...}

    -- 政策旗標
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,   -- 全域 fallback；最多一筆
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,    -- 軟停用（不在熱路徑被取用）

    -- 稽核欄位
    notes           TEXT NULL,
    created_by      BIGINT NULL REFERENCES users(id),
    approved_by     BIGINT NULL REFERENCES users(id),
    approved_at     TIMESTAMPTZ NULL,

    -- AI 輔助痕跡（W3 用，先建好欄位避免之後 ALTER）
    llm_assisted    BOOLEAN NOT NULL DEFAULT FALSE,
    llm_model       TEXT NULL,                -- 例：'gpt-4o-2024-08-06'、'claude-opus-4-6'
    llm_prompt_hash TEXT NULL,                -- 對應 prompt 的 sha256（出庭時可重現推導路徑）

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 全域 default profile 最多一筆（partial unique index）
CREATE UNIQUE INDEX IF NOT EXISTS idx_carrier_profiles_one_default
    ON carrier_profiles (is_default) WHERE is_default = TRUE;

-- W2 模板指紋查詢用
CREATE INDEX IF NOT EXISTS idx_carrier_profiles_fingerprint
    ON carrier_profiles (fingerprint) WHERE fingerprint IS NOT NULL;

-- 一般列表查詢
CREATE INDEX IF NOT EXISTS idx_carrier_profiles_active
    ON carrier_profiles (is_active, carrier_name);

-- ---------- Seed：default profile（把現行 _RAW2CANON 灌進去 + 補新樣本檔別名） ----------
-- 為什麼要在 schema 內種子化：
--   ingest.py 在 W1 後改成「從 DB 讀 mapping」；若 DB 沒有任何 default profile，
--   程式會 fallback 回 code 內的 _RAW2CANON 常數。為了讓「DB 是 SoT (source of truth)」
--   名實相符，schema apply 完就要有一筆 default profile 在線。
--
-- 冪等性保證：用 NOT EXISTS 條件式 INSERT，重跑 schema.sql 不會建出第二筆。
--
-- 別名來源：
--   1. ingest.py:_RAW2CANON 既有 36 個鍵
--   2. 4 個真實樣本檔新增（已驗證能對齊 schema 欄位）：
--      - 「時間」「始話時間」「通聯時間」 → start_ts
--      - 「基地台」「基地台/交換機」「起台」 → cell_id
--      - 「起址」 → cell_addr
--   3. 暫不收的別名（記在 notes，等 W2/W3 加 schema 欄位）：
--      - 「迄台」「迄址」「迄基地台」「終話基地台」（通話記錄需要 cell_id_end / cell_addr_end）
--      - 「始話日期」（與「始話時間」拆兩欄，需要 ingest 層做合併規則）
INSERT INTO carrier_profiles (
    variant_label, mapping_json, is_default, notes, created_by, approved_by, approved_at
)
SELECT
    'default',
    $${
      "開始連線時間": "start_ts",
      "結束連線時間": "end_ts",
      "開始時間":     "start_ts",
      "結束時間":     "end_ts",
      "起始時間":     "start_ts",
      "啟始時間":     "start_ts",
      "終止時間":     "end_ts",
      "時間":         "start_ts",
      "始話時間":     "start_ts",
      "通聯時間":     "start_ts",

      "基地台地址":     "cell_addr",
      "基地臺地址":     "cell_addr",
      "基地台位址":     "cell_addr",
      "基地臺位址":     "cell_addr",
      "最終基地台位址": "cell_addr",
      "最終基地臺位址": "cell_addr",
      "站台地址":       "cell_addr",
      "地址":           "cell_addr",
      "起址":           "cell_addr",

      "基地台編號":   "cell_id",
      "基地臺編號":   "cell_id",
      "基地台ID":     "cell_id",
      "基地臺ID":     "cell_id",
      "最終基地台ID": "cell_id",
      "最終基地臺ID": "cell_id",
      "站台編號":     "cell_id",
      "站碼":         "cell_id",
      "cell_id":      "cell_id",
      "基地台":         "cell_id",
      "基地台/交換機": "cell_id",
      "起台":           "cell_id",

      "細胞名稱":   "sector_name",
      "小區名稱":   "sector_name",
      "台號":       "site_code",
      "站號":       "site_code",
      "站名":       "site_code",
      "細胞":       "sector_id",
      "小區":       "sector_id",
      "cell":       "sector_id",
      "方位":       "azimuth",
      "方位角":     "azimuth",
      "azimuth":    "azimuth"
    }$$::jsonb,
    TRUE,
    '系統預設 fallback profile：搬遷自 ingest.py:_RAW2CANON（36 個別名）+ 4 個真實樣本檔新增別名（時間/始話時間/通聯時間/基地台/基地台-斜線-交換機/起台/起址）。'
    || E'\n暫不收的別名（待 W2/W3 處理）：迄台、迄址、迄基地台、終話基地台（缺 cell_id_end 欄位）；始話日期（需與始話時間合併規則）。',
    NULL, NULL, NULL
WHERE NOT EXISTS (
    SELECT 1 FROM carrier_profiles WHERE is_default = TRUE
);

-- ---------- Seed：初始管理員 ----------
-- 僅在 users 表為空時建立預設 admin 帳號；
-- 預設帳號：admin / admin123（上線前請務必修改）
INSERT INTO users (username, password_hash, role)
SELECT 'admin', crypt('admin123', gen_salt('bf', 12)), 'admin'
WHERE NOT EXISTS (SELECT 1 FROM users);
