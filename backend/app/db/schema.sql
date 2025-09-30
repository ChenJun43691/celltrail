-- Enable PostGIS
CREATE EXTENSION IF NOT EXISTS postgis;

-- Raw traces table
CREATE TABLE IF NOT EXISTS raw_traces (
  id          BIGSERIAL PRIMARY KEY,
  project_id  TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  start_ts    TIMESTAMPTZ,
  end_ts      TIMESTAMPTZ,
  cell_id     TEXT,
  cell_addr   TEXT,
  azimuth     INT,
  accuracy_m  INT,
  geom        geometry(Point, 4326)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_raw_traces_start_ts ON raw_traces (start_ts);
CREATE INDEX IF NOT EXISTS idx_raw_traces_geom     ON raw_traces USING GIST (geom);
