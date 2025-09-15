import os
from psycopg_pool import ConnectionPool

DB_URL = os.getenv("DATABASE_URL", "postgresql://celltrail:celltrail@localhost:5432/celltrail")
pool = ConnectionPool(DB_URL, min_size=1, max_size=10, kwargs={"autocommit": True})

-- 安全保險：確保 PostGIS 擴充存在
CREATE EXTENSION IF NOT EXISTS postgis;

-- 原始網路歷程表（先放 MVP 欄位）
CREATE TABLE IF NOT EXISTS raw_traces (
  id            BIGSERIAL PRIMARY KEY,
  project_id    TEXT NOT NULL,
  target_id     TEXT NOT NULL,
  msisdn        TEXT,
  imei          TEXT,
  start_ts      TIMESTAMPTZ NOT NULL,
  end_ts        TIMESTAMPTZ NOT NULL,
  cell_id       TEXT,
  cell_addr     TEXT,
  sector_name   TEXT,
  site_code     TEXT,
  sector_id     TEXT,
  azimuth       INT,
  lat           DOUBLE PRECISION,
  lng           DOUBLE PRECISION,
  accuracy_m    INT,
  geom          geometry(Point, 4326)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_raw_traces_start_ts ON raw_traces (start_ts);
CREATE INDEX IF NOT EXISTS idx_raw_traces_geom ON raw_traces USING GIST (geom);