-- migration_preview_artifacts.sql（P9A A.2，2026-07-01）
-- Preview evidence artifact 暫存表：儲存 preview 的「加密原始檔 + 雙 hash + provenance」，
-- 供「儲存為專案」時 server 端重解析為權威來源（補 save-records 的證據鏈缺口）。
--
-- 設計：
--   - internal id = BIGSERIAL PK（未來 FK 綁這個）；external preview_id = token（不當 FK）。
--   - raw_enc = crypto_box AES-256-GCM(gzip(raw))（storage_kind='db'）；
--     storage_kind='object' 走 object storage（A.5，本表以 storage_key 記 key）。
--   - parsed_records_hash = canonical-JSON SHA-256（deterministic）。
--   - system_sealed_at = create 時系統背書；sealed_* = analyst；supervisor_* 留欄待 P9B。
--   - 短 TTL（expires_at）+ 背景清理；無 read_count（讀取次數由 audit_logs 導出）。
--
-- 冪等：CREATE TABLE / INDEX IF NOT EXISTS，重跑安全。
-- 注意：preview_artifact.py 的 _ensure_preview_table() 也會自動建立此表，故新環境忘了套
-- 也不會壞；本檔供「明確、可審計建立」之用。

CREATE TABLE IF NOT EXISTS preview_artifacts (
  id                    BIGSERIAL   PRIMARY KEY,
  preview_id            TEXT UNIQUE NOT NULL,             -- external token（secrets.token_urlsafe(24)）
  filename              TEXT        NOT NULL,
  ext                   TEXT        NULL,
  size_bytes            BIGINT      NOT NULL,
  sha256_full           TEXT        NOT NULL,             -- 原始檔 SHA-256（deterministic）
  parsed_records_hash   TEXT        NOT NULL,             -- canonical-JSON SHA-256
  row_count             INT         NOT NULL,
  storage_kind          TEXT        NOT NULL,             -- 'db' | 'object'
  raw_enc               BYTEA       NULL,                 -- storage_kind='db'：AES-256-GCM(gzip(raw))
  storage_key           TEXT        NULL,                 -- storage_kind='object'：bucket key（A.5）
  enc_alg               TEXT        NOT NULL,             -- 'aesgcm-v1'
  parser_type           TEXT        NOT NULL,             -- telecom_canonical|carrier_profile|simple_time_location|pdf|...
  provenance            JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_by            BIGINT      NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at            TIMESTAMPTZ NOT NULL,             -- created_at + PREVIEW_TTL_MIN（server 寫死）
  system_sealed_at      TIMESTAMPTZ NULL,                 -- create 時系統背書
  sealed_at             TIMESTAMPTZ NULL,                 -- analyst seal
  sealed_by             BIGINT      NULL,
  supervisor_sealed_at  TIMESTAMPTZ NULL,                 -- P9B（只留欄）
  supervisor_sealed_by  BIGINT      NULL,
  consumed_at           TIMESTAMPTZ NULL,                 -- 已 persist 成 raw_traces
  consumed_project      TEXT        NULL,
  consumed_target       TEXT        NULL,
  revoked_at            TIMESTAMPTZ NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_preview_artifacts_pid     ON preview_artifacts (preview_id);
CREATE INDEX        IF NOT EXISTS idx_preview_artifacts_expires ON preview_artifacts (expires_at);
CREATE INDEX        IF NOT EXISTS idx_preview_artifacts_creator ON preview_artifacts (created_by);
