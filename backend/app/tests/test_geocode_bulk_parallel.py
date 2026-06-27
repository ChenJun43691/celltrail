# backend/app/tests/test_geocode_bulk_parallel.py
"""
lookup_bulk 並行 geocode（2026-06-27）

Background:
- 雲端無 Redis 快取 + cell_towers 空時，大檔（test3 ~2400 唯一地址）原本逐筆
  序列打 Google，累積 >110s → 超過 Render 請求上限回 502。
- 修法：Step 5 改用 ThreadPoolExecutor 並行打 Google；Google 失敗者才走 OSM
  序列備援（Nominatim 1 req/s 政策不可並行）。並對 simplified 地址去重。

本檔 monkeypatch _google_geocode / _osm_geocode，不打真網路、不碰 DB
（全用 cell_id=None 的 key 以略過 cell_towers SQL 查詢）。
"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


def test_bulk_parallel_dedup_and_mapping(monkeypatch):
    """並行打 Google + 去重 + orig_key 正確回對。"""
    import app.services.geocode as geo

    calls = []
    lock = threading.Lock()

    def fake_google(addr):
        with lock:
            calls.append(addr)
        time.sleep(0.05)  # 模擬網路延遲，凸顯並行效果
        return (25.0, 121.5)

    monkeypatch.setattr(geo, "_google_geocode", fake_google)
    monkeypatch.setattr(geo, "_osm_geocode", lambda a: None)

    # 5 個 key，但其中兩個地址清洗後同址 → 應只打 4 次 Google
    keys = [
        (None, "台北市信義區A路1號"),
        (None, "台北市信義區A路1號5樓"),   # 清洗後同上 → 去重
        (None, "台北市大安區B路2號"),
        (None, "高雄市前金區C路3號"),
        (None, "台中市西區D路4號"),
    ]
    t0 = time.perf_counter()
    out = geo.lookup_bulk(keys)
    elapsed = time.perf_counter() - t0

    # 去重：4 個唯一 simplified 地址
    assert len(set(calls)) == 4
    assert len(calls) == 4, f"應去重後只打 4 次，實際 {len(calls)}"

    # 每個 orig_key 都拿到座標（含被去重的那個）
    for k in keys:
        assert out[k] == (25.0, 121.5), f"{k} 未正確回對"

    # 並行：4 次 × 0.05s 序列會是 0.2s，並行應明顯更短
    assert elapsed < 0.15, f"應並行執行，實際耗時 {elapsed:.3f}s"


def test_bulk_osm_fallback_sequential(monkeypatch):
    """Google 失敗者走 OSM 備援，且 OSM 仍被呼叫到。"""
    import app.services.geocode as geo

    def fake_google(addr):
        # 只有 B 路 Google 查得到，其餘失敗
        return (25.0, 121.5) if "B路" in addr else None

    osm_called = []

    def fake_osm(addr):
        osm_called.append(addr)
        return (24.0, 120.0)

    monkeypatch.setattr(geo, "_google_geocode", fake_google)
    monkeypatch.setattr(geo, "_osm_geocode", fake_osm)

    keys = [
        (None, "台北市A路1號"),   # Google 失敗 → OSM
        (None, "台北市B路2號"),   # Google 成功
        (None, "台北市C路3號"),   # Google 失敗 → OSM
    ]
    out = geo.lookup_bulk(keys)

    assert sorted(osm_called) == ["台北市A路1號", "台北市C路3號"]
    assert out[(None, "台北市B路2號")] == (25.0, 121.5)   # Google
    assert out[(None, "台北市A路1號")] == (24.0, 120.0)   # OSM
    assert out[(None, "台北市C路3號")] == (24.0, 120.0)   # OSM


def test_bulk_empty_and_no_addr(monkeypatch):
    """空輸入回空；無地址且無 cell_id 的 key 回 None、不打 Google。"""
    import app.services.geocode as geo

    called = []
    monkeypatch.setattr(geo, "_google_geocode", lambda a: called.append(a) or (1.0, 2.0))
    monkeypatch.setattr(geo, "_osm_geocode", lambda a: None)

    assert geo.lookup_bulk([]) == {}

    out = geo.lookup_bulk([(None, ""), (None, None)])
    assert out[(None, "")] is None
    assert out[(None, None)] is None
    assert called == [], "無地址不應打 Google"


def test_concurrency_env_default():
    import app.services.geocode as geo

    assert geo.GEO_GOOGLE_CONCURRENCY >= 1
