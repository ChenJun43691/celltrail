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
--      - 「迄台」「迄址」（單純迄話端，需要 cell_id_end / cell_addr_end，待未來 milestone）
--      - 「始話日期」（與「始話時間」拆兩欄，需要 ingest 層做合併規則）
--
-- W2.4 設計筆記（2026-04-29）：方言（dialect）系統 vs 全域 alias
--   _RAW2CANON 內「起台 → cell_id」「起址 → cell_addr」這兩條對應是 W1
--   階段對「起 = 起點」的誤解（中華上網方言實際上「起台 = 時間戳、起址
--   = cell_id」，語意相反）。但這兩條對應**保留不動**的理由：
--     - _iter_rows_excel 的 header detection 用 active_map 計分，移除這
--       兩條會讓周蔓達.xlsx 的真表頭命中分數降到 0、整個 sheet 被跳過
--     - 設計改採「保留全域 alias 當 header detection 訊號 + 用 dialect
--       override map 當實際 normalize 規則」雙層架構，兩者解耦
--   實際正確的對應由 ingest.py:_DIALECT_HEADER_MAPS["cht_internet"] 接管，
--   detector 命中時走 _normalize_row_dialect 整批替換 header_map。
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
      "始話日期時間": "start_ts",
      "進入基地台時間": "start_ts",
      "離開基地台時間": "end_ts",
      "通聯時間":     "start_ts",
      "手機連到基地台的時間": "start_ts",
      "連到internet的時間":   "start_ts",

      "基地台地址":     "cell_addr",
      "基地臺地址":     "cell_addr",
      "基地台位址":     "cell_addr",
      "基地臺位址":     "cell_addr",
      "最終基地台位址": "cell_addr",
      "最終基地臺位址": "cell_addr",
      "站台地址":       "cell_addr",
      "地址":           "cell_addr",
      "起址":           "cell_addr",
      "離開基地台地址": "cell_addr",

      "基地台編號":   "cell_id",
      "基地臺編號":   "cell_id",
      "基地台ID":     "cell_id",
      "基地臺ID":     "cell_id",
      "最終基地台ID": "cell_id",
      "最終基地臺ID": "cell_id",
      "站台編號":     "cell_id",
      "站碼":         "cell_id",
      "離開基地台編號": "cell_id",
      "cell_id":      "cell_id",
      "基地台":         "cell_id",
      "基地台/交換機": "cell_id",
      "起台":           "cell_id",
      "基地台代碼":     "cell_id",

      "迄基地台":   "cell_id_compound",
      "終話基地台": "cell_id_compound",
      "基地台編號1/位置1": "cell_id_compound",
      "基地台編號2/位置2": "cell_id_compound",

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
      "azimuth":    "azimuth",

      "GPS時間":      "start_ts",
      "gps時間":      "start_ts",
      "定位時間":     "start_ts",
      "經度":         "lng",
      "緯度":         "lat",
      "經度(wgs84)":  "lng",
      "緯度(wgs84)":  "lat",
      "longitude":    "lng",
      "latitude":     "lat",
      "lng":          "lng",
      "lat":          "lat",
      "lon":          "lng"
    }$$::jsonb,
    TRUE,
    '系統預設 fallback profile：搬遷自 ingest.py:_RAW2CANON（36 個別名）+ 4 個真實樣本檔新增別名（時間/始話時間/通聯時間/基地台/基地台-斜線-交換機/起台/起址）+ W2.2 網路歷程方言（手機連到基地台的時間/連到internet的時間/基地台代碼）+ W2.3 複合欄（迄基地台/終話基地台 → cell_id_compound，由 _normalize_row 拆解）+ W2.4 dialect 系統（中華上網方言由 ingest.py:_DIALECT_HEADER_MAPS 接管，「起台→start_ts、起址→cell_id、通話對象→cell_addr」此處 alias 保留為 header detection 訊號）。'
    || E'\n+ GPS 軌跡/經緯度直給格式（2026-06-04）：GPS時間/定位時間→start_ts、經度→lng、緯度→lat（含 wgs84 後綴與英文 longitude/latitude/lng/lat/lon）。此類檔無 cell_id/地址，ingest 直接採用座標免 geocode；經緯度欄常被標反，由 _resolve_latlng 以「緯度必在 [-90,90]」範圍自動校正。'
    || E'\n+ 雙向通聯格式（2026-06-22）：始話日期時間→start_ts；基地台編號1/位置1、基地台編號2/位置2→cell_id_compound（cell_id 與地址用 "/" 或全形「／」合併，由 _split_compound_cell 斜線分支拆解）。'
    || E'\n+ 台哥大上網歷程格式（2026-06-27，test2.xlsx）：進入基地台時間→start_ts、離開基地台時間→end_ts、離開基地台編號→cell_id、離開基地台地址→cell_addr（純 ID/地址欄，非複合欄）。此格式表頭埋在 row 27（前置查詢條件 + 完整使用者資料 PII 區塊），故 SCAN_WINDOW 由 25 放寬至 30。'
    || E'\n暫不收的別名（待未來 milestone）：迄台、迄址（單純迄話端，需先補 cell_id_end / cell_addr_end 欄位）；始話日期（需與始話時間合併規則）。',
    NULL, NULL, NULL
WHERE NOT EXISTS (
    SELECT 1 FROM carrier_profiles WHERE is_default = TRUE
);

-- ---------- Geocode 持久快取（2026-06-27） ----------
-- 地址 → 座標的跨請求快取，取代雲端失效的 Redis。
--   - 用途：大檔上傳時 geocode 結果持久化，首傳分批灌、重傳跳過已快取者，
--     避免每次都重打 Google 而超過 Render 120s 請求上限 502。
--   - addr 為「清洗後」地址（_simplify_addr 結果）；ON CONFLICT 冪等。
--   - geocode.py 內 _ensure_sql_cache() 也會 CREATE TABLE IF NOT EXISTS 自動建立，
--     此處列入 schema 供新環境完整建立與文件化。
CREATE TABLE IF NOT EXISTS geocode_cache (
    addr        TEXT PRIMARY KEY,
    lat         DOUBLE PRECISION NOT NULL,
    lng         DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Preview Evidence Artifact（P9A，2026-07-01） ----------
-- preview 的「加密原始檔 + 雙 hash + provenance」暫存表；供「儲存為專案」時 server
-- 端重解析為權威來源，補 save-records 的證據鏈缺口。詳見 migration_preview_artifacts.sql。
--   - internal id = BIGSERIAL；external preview_id = token（不當 FK）。
--   - raw_enc = AES-256-GCM(gzip(raw))（crypto_box）；短 TTL（expires_at）+ 背景清理。
--   - preview_artifact.py 的 _ensure_preview_table() 也會自動建立此表（雲端免手動）。
CREATE TABLE IF NOT EXISTS preview_artifacts (
  id                    BIGSERIAL   PRIMARY KEY,
  preview_id            TEXT UNIQUE NOT NULL,
  filename              TEXT        NOT NULL,
  ext                   TEXT        NULL,
  size_bytes            BIGINT      NOT NULL,
  sha256_full           TEXT        NOT NULL,
  parsed_records_hash   TEXT        NOT NULL,
  row_count             INT         NOT NULL,
  storage_kind          TEXT        NOT NULL,
  raw_enc               BYTEA       NULL,
  storage_key           TEXT        NULL,
  enc_alg               TEXT        NOT NULL,
  parser_type           TEXT        NOT NULL,
  provenance            JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_by            BIGINT      NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at            TIMESTAMPTZ NOT NULL,
  system_sealed_at      TIMESTAMPTZ NULL,
  sealed_at             TIMESTAMPTZ NULL,
  sealed_by             BIGINT      NULL,
  supervisor_sealed_at  TIMESTAMPTZ NULL,
  supervisor_sealed_by  BIGINT      NULL,
  consumed_at           TIMESTAMPTZ NULL,
  consumed_project      TEXT        NULL,
  consumed_target       TEXT        NULL,
  revoked_at            TIMESTAMPTZ NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_preview_artifacts_pid     ON preview_artifacts (preview_id);
CREATE INDEX        IF NOT EXISTS idx_preview_artifacts_expires ON preview_artifacts (expires_at);
CREATE INDEX        IF NOT EXISTS idx_preview_artifacts_creator ON preview_artifacts (created_by);

-- ---------- Cell Tower Reference Table（P4.1） ----------
-- 本地基地台座標對照表：cell_id → lat/lng
-- 用途：geocode 前置查詢，命中即直接用，不打 Google/OSM API
--   - 來源：電信業者提供的基地台座標 CSV
--   - ON CONFLICT(cell_id) DO UPDATE：重新匯入同份資料會覆蓋，冪等安全
--   - carrier_name 可為 NULL（業者不明或混合匯入時）
CREATE TABLE IF NOT EXISTS cell_towers (
    id           BIGSERIAL PRIMARY KEY,
    cell_id      TEXT NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lng          DOUBLE PRECISION NOT NULL,
    carrier_name TEXT NULL,       -- 業者名稱（中華電信 / 台哥大 / 遠傳 / ...）
    source       TEXT NULL,       -- 來源描述（如 "CHT 2024Q1 基地台座標表"）
    memo         TEXT NULL,
    imported_by  BIGINT NULL REFERENCES users(id),
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cell_towers_cell_id
    ON cell_towers (cell_id);
CREATE INDEX IF NOT EXISTS idx_cell_towers_carrier
    ON cell_towers (carrier_name);

-- ---------- 格式回報（使用者上傳無法解析的檔案 → 回報管理員加新方言） ----------
-- 設計考量：
--   - 不存原始檔案內容（隱私 + 容量考量），只存 headers 清單與診斷結果
--   - status: 'open' | 'handled' | 'rejected'；admin 加入新方言後改 handled
--   - reporter_user_id 可為 null（訪客也能回報）
CREATE TABLE IF NOT EXISTS format_reports (
    id              BIGSERIAL PRIMARY KEY,
    filename        TEXT NOT NULL,
    headers         JSONB NOT NULL,
    diagnosis       JSONB NOT NULL,
    note            TEXT NULL,
    reporter_user_id BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
    reporter_ip     INET NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'handled', 'rejected')),
    handled_by      BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
    handled_at      TIMESTAMPTZ NULL,
    handled_note    TEXT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_format_reports_status ON format_reports (status);
CREATE INDEX IF NOT EXISTS idx_format_reports_created ON format_reports (created_at DESC);

-- ---------- users 擴充欄位（供下方 Seed 使用，修正套用順序陷阱）----------
-- 這幾個欄位的「正式定義」在 migration_permissions.sql；此處重複宣告是為了
-- 讓 schema.sql 自足 —— 下方 Seed 的 INSERT 會用到 real_name / is_active /
-- must_change_password，若在全新 DB 上嚴格「先 schema 後 migration」，會因
-- 欄位尚未存在而種不出 admin 帳號（psql 預設不中止、會跳過該 INSERT）。
-- ADD COLUMN IF NOT EXISTS 為冪等，欄位定義與 migration_permissions.sql 完全
-- 一致，兩邊並存無害。
ALTER TABLE users ADD COLUMN IF NOT EXISTS real_name            TEXT    NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active            BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;

-- ---------- Seed：初始管理員（防禦縱深，2026-06-13）----------
-- 安全原則：刻意「不」在公開 repo 放真實密碼。兩個種子帳號都用一次性 placeholder
-- 密碼 + must_change_password=TRUE，新部署首次登入即被導向強制改密碼。
--   ⚠ 正式部署完成後務必：(1) 立即以 placeholder 登入並改成強密碼；
--     (2) 視情況停用內建帳號、另建專屬帳號（docs/部署檢查清單.md A 區）。
--   背景：舊版種子把真實密碼（CIDadmin/436910619、admin/admin123）明寫在公開
--     repo 且 must_change_password=FALSE → 任何人讀 repo 即可登入 admin
--     （2026-06-13 資安檢視於雲端實測命中，已修；雲端帳號密碼亦已輪替）。
-- placeholder 密碼（首次登入即須更換）：
--   CIDadmin → 'CellTrail-SetMe-OnFirstLogin'
--   admin    → 'CellTrail-Dev-Only-ChangeMe'（僅本機開發用，正式環境停用）
INSERT INTO users (username, password_hash, role, real_name, is_active, must_change_password)
SELECT 'CIDadmin', crypt('CellTrail-SetMe-OnFirstLogin', gen_salt('bf', 12)), 'admin', '系統管理員', TRUE, TRUE
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username='CIDadmin');

INSERT INTO users (username, password_hash, role, is_active, must_change_password)
SELECT 'admin', crypt('CellTrail-Dev-Only-ChangeMe', gen_salt('bf', 12)), 'admin', TRUE, TRUE
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username='admin');
