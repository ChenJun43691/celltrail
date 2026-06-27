-- migration_geocode_cache.sql（2026-06-27）
-- Geocode 持久快取表：取代雲端失效的 Redis，讓 geocode 結果跨請求保存。
--
-- 背景：雲端（Render + Supabase）移除 Redis 後，geocode 結果不跨請求保存，
-- 每次大檔上傳都重打 Google → 累積超過 Render 120s 請求上限回 502。
-- 本表讓結果持久化：首次上傳分批灌、之後（含逾時重傳）跳過已快取者 → 漸進
-- 變快、最終必然成功。
--
-- 冪等：CREATE TABLE IF NOT EXISTS，重跑安全。
-- 注意：geocode.py 的 _ensure_sql_cache() 也會在執行期自動建立此表，故新環境
-- 即使忘了套這支 migration 也不會壞；此檔供「明確、可審計地建立」之用。

CREATE TABLE IF NOT EXISTS geocode_cache (
    addr        TEXT PRIMARY KEY,
    lat         DOUBLE PRECISION NOT NULL,
    lng         DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
