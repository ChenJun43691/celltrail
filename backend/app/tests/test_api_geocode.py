# backend/app/tests/test_api_geocode.py
"""
`GET /api/geocode`（前端「📍 自訂標記」的定位 / 新增標記用）。

背景 —— 2026-08-27 使用者回報「定位失敗」，查出來的不只是金鑰過期：
這支端點原本是**第五條獨立的地理編碼實作**，直接打 Google，於是

  ① 拿不到 cell_towers / Redis / SQL 快取 / TGOS / OSM 任何一層；
  ② **不理會 `GEO_GOOGLE_ENABLED`** —— 那個為了止住費用而加的硬止血開關對它無效；
  ③ 不需登入也沒有速率限制，而自訂標記面板訪客看得到
     → 等於對外開放一支以本專案帳單支付的 Google 代理；
  ④ 失敗時把上游的 `REQUEST_DENIED` 原樣丟給使用者。

改為委派 `services.geocode.lookup()` 後，本檔守住這四點不再回頭。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
import app.services.geocode as geo

app = main_mod.app
client = TestClient(app)
URL = "/api/geocode"


@pytest.fixture(autouse=True)
def _reset():
    # slowapi 用共用 in-memory storage，不重設會讓測試之間互相吃掉配額。
    try:
        from app.services.limiter import limiter
        limiter.limiter.storage.reset()
    except Exception:
        pass
    yield


def test_delegates_to_service_lookup(monkeypatch):
    """必須走 services.geocode.lookup —— 那才拿得到 cell_towers 與各層快取。

    系統裡已經有幾千筆基地台座標，舊實作卻一律重新問 Google。
    """
    seen = []
    monkeypatch.setattr(geo, "lookup", lambda cid, addr: seen.append((cid, addr)) or (22.6, 120.3))
    r = client.get(URL, params={"address": "高雄市苓雅區四維三路2號"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["lat"], j["lng"]) == (22.6, 120.3)
    assert seen == [(None, "高雄市苓雅區四維三路2號")], "應以 cell_id=None 做純地址查詢"


def test_response_shape_kept_for_frontend(monkeypatch):
    """前端讀 lat / lng / formatted_address —— 改寫後這三個欄位必須都還在，
    否則自訂標記會從『定位失敗』變成『畫面沒反應』，更難查。"""
    monkeypatch.setattr(geo, "lookup", lambda cid, addr: (25.0, 121.5))
    j = client.get(URL, params={"address": "台北市信義區市府路1號"}).json()
    assert {"query", "formatted_address", "lat", "lng"} <= set(j)
    assert j["formatted_address"], "查詢鏈不回正規化地址時要回填輸入值，不可為空"


def test_not_found_explains_why_when_all_sources_disabled(monkeypatch):
    """三個來源全停用時，錯誤訊息要說出這件事。

    舊版直接把上游的 `geocode failed: REQUEST_DENIED` 丟給使用者，
    畫面顯示成「定位失敗：geocode failed: REQUEST_DENIED」——
    使用者無從得知真正的原因是「地址定位來源全被關掉了」。
    """
    monkeypatch.setattr(geo, "lookup", lambda cid, addr: None)
    monkeypatch.setattr(geo, "_tgos_enabled", lambda: False)
    monkeypatch.setattr(geo, "_google_enabled", lambda: False)
    monkeypatch.setattr(geo, "USE_OSM", False)
    r = client.get(URL, params={"address": "查無此地"})
    assert r.status_code == 404
    d = r.json()["detail"]
    assert "全數停用" in d and "基地台座標表" in d
    assert "REQUEST_DENIED" not in d, "不得把上游錯誤碼原樣外洩給使用者"


def test_not_found_lists_attempted_sources(monkeypatch):
    """有來源啟用時，訊息改成「已嘗試哪些」—— 這時問題比較可能出在地址本身。"""
    monkeypatch.setattr(geo, "lookup", lambda cid, addr: None)
    monkeypatch.setattr(geo, "_tgos_enabled", lambda: False)
    monkeypatch.setattr(geo, "_google_enabled", lambda: True)
    monkeypatch.setattr(geo, "USE_OSM", True)
    d = client.get(URL, params={"address": "x"}).json()["detail"]
    assert "Google" in d and "OSM" in d


def test_respects_google_kill_switch(monkeypatch):
    """`GEO_GOOGLE_ENABLED=0` 時絕不可送出 Google 請求。

    這正是舊實作最危險的地方：那個開關是為了止住費用而加的，卻止不到這支端點。
    現在因為委派給 services.geocode，開關自然生效 —— 本測試防止有人日後又
    「為了方便」在這裡直接打 Google。
    """
    monkeypatch.setenv("GEO_GOOGLE_ENABLED", "0")
    monkeypatch.setattr(geo, "USE_OSM", False)
    monkeypatch.setattr(geo, "_lookup_from_local", lambda cid, addr: None)
    monkeypatch.setattr(geo, "_cache_get", lambda a: None)
    monkeypatch.setattr(geo, "_sql_cache_get_bulk", lambda addrs: {})
    monkeypatch.setattr(geo.requests, "get",
                        lambda *a, **k: pytest.fail("Google 停用時不該發出任何 HTTP 請求"))
    assert client.get(URL, params={"address": "高雄市苓雅區四維三路2號"}).status_code == 404


def test_blank_address_rejected(monkeypatch):
    monkeypatch.setattr(geo, "lookup", lambda cid, addr: None)
    assert client.get(URL, params={"address": "   "}).status_code == 400


def test_is_rate_limited(monkeypatch):
    """端點無需登入且下游可能計費 → 必須限流，否則是一支對外開放的付費代理。"""
    monkeypatch.setattr(geo, "lookup", lambda cid, addr: (22.6, 120.3))
    codes = {client.get(URL, params={"address": f"a{i}"}).status_code for i in range(70)}
    assert 429 in codes, f"未觸發限流：{codes}"
