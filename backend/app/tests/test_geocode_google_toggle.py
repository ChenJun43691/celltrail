# backend/app/tests/test_geocode_google_toggle.py
"""
GEO_GOOGLE_ENABLED 硬止血開關測試（2026-07-03）。

證明：GEO_GOOGLE_ENABLED=0 時，系統不建立任何送往 Google 的 HTTP request、
也不提交任何 Google geocode ThreadPool task；查詢改走 cell_towers → SQL cache
→（Google 跳過）→ OSM → unlocated。

策略：call-time helper `_google_enabled()` 讀 env（monkeypatch.setenv 可靠生效）；
module-level 常數（GMAPS_KEY/USE_OSM）用 monkeypatch.setattr；不碰真 DB / 真網路。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

import app.services.geocode as geo


@pytest.fixture(autouse=True)
def _no_sql_cache(monkeypatch):
    """停用 SQL 快取（避免碰 DB）；個別測試可覆寫。"""
    monkeypatch.setattr(geo, "_sql_cache_get_bulk", lambda addrs: {})
    monkeypatch.setattr(geo, "_sql_cache_set_bulk", lambda items: None)


def _http_boom(*a, **k):
    raise AssertionError("不應建立 Google HTTP request")


# ── E. 開關值解析 ────────────────────────────────────────────
@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", "  0  ", " Off ", "No"])
def test_disabled_values(monkeypatch, val):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", val)
    assert geo._google_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "", "garbage", " 1 "])
def test_enabled_values(monkeypatch, val):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", val)
    assert geo._google_enabled() is True


def test_default_unset_is_enabled(monkeypatch):
    monkeypatch.delenv("GEO_GOOGLE_ENABLED", raising=False)
    assert geo._google_enabled() is True


# ── A. disabled 單筆零 HTTP ──────────────────────────────────
def test_disabled_single_zero_http(monkeypatch):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", "0")
    monkeypatch.setattr(geo, "GMAPS_KEY", "a-real-looking-key")   # key 有值
    monkeypatch.setattr(geo.requests, "get", _http_boom)          # 一旦送出即 fail
    assert geo._google_geocode("台北市信義區市府路1號") is None    # 立即回 None、未打 HTTP


# ── B. disabled bulk 零 Google task ─────────────────────────
def test_disabled_bulk_no_google_task(monkeypatch):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", "0")
    monkeypatch.setattr(geo, "_google_geocode",
                        lambda a: pytest.fail("_google_geocode 不應被呼叫"))
    monkeypatch.setattr(geo, "_osm_geocode", lambda a: None)      # OSM 也查不到
    out = geo.lookup_bulk([(None, "台北市A路1號")])
    assert out[(None, "台北市A路1號")] is None                     # 走到 unlocated


# ── C. disabled + OSM enabled → 走 OSM、Google 不呼叫 ────────
def test_disabled_osm_enabled(monkeypatch):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", "0")
    monkeypatch.setattr(geo, "_google_geocode",
                        lambda a: pytest.fail("Google 不應被呼叫"))
    osm_calls = []
    monkeypatch.setattr(geo, "_osm_geocode",
                        lambda a: (osm_calls.append(a) or (24.0, 120.0)))
    out = geo.lookup_bulk([(None, "台北市A路1號")])
    assert osm_calls == ["台北市A路1號"]
    assert out[(None, "台北市A路1號")] == (24.0, 120.0)


# ── D. disabled + OSM disabled → 皆不呼叫、零 HTTP、不 crash ──
def test_disabled_osm_disabled_zero_http(monkeypatch):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", "0")
    monkeypatch.setattr(geo, "GMAPS_KEY", "key")
    monkeypatch.setattr(geo, "USE_OSM", False)                    # OSM 關閉（真 _osm_geocode 早退）
    monkeypatch.setattr(geo.requests, "get", _http_boom)          # 任何 HTTP → fail
    out = geo.lookup_bulk([(None, "台北市A路1號")])
    assert out[(None, "台北市A路1號")] is None                     # unlocated，零外部呼叫


# ── F. 預設向後相容：未設 env + key 有值 → Google 路徑仍可跑 ──
def test_default_google_path_runs(monkeypatch):
    monkeypatch.delenv("GEO_GOOGLE_ENABLED", raising=False)
    monkeypatch.setattr(geo, "GMAPS_KEY", "key")
    called = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "OK", "results": [{"geometry": {"location": {"lat": 25.0, "lng": 121.5}}}]}

    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: (called.append(1) or _Resp()))
    assert geo._google_geocode("台北市") == (25.0, 121.5)
    assert called == [1]                                          # 向後相容：Google 確實被呼叫


# ── G. local/cache 命中 → 不論開關，皆不呼叫外部 ────────────
def test_cache_hit_no_external_regardless(monkeypatch):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", "1")                 # 即使啟用
    monkeypatch.setattr(geo, "_google_geocode",
                        lambda a: pytest.fail("cache 命中不應打 Google"))
    monkeypatch.setattr(geo, "_osm_geocode",
                        lambda a: pytest.fail("cache 命中不應打 OSM"))
    monkeypatch.setattr(geo, "_sql_cache_get_bulk",
                        lambda addrs: {"台北市A路1號": (22.6, 120.3)})
    out = geo.lookup_bulk([(None, "台北市A路1號")])
    assert out[(None, "台北市A路1號")] == (22.6, 120.3)            # 來自快取，零外部呼叫


# ── enabled + key missing / API error 維持既有 fallback（回歸） ─
def test_enabled_key_missing_returns_none(monkeypatch):
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", "1")
    monkeypatch.setattr(geo, "GMAPS_KEY", None)
    monkeypatch.setattr(geo.requests, "get", _http_boom)          # key 缺 → 不該打 HTTP
    assert geo._google_geocode("台北市") is None
