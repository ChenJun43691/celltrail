# backend/app/tests/test_geocode_tgos.py
"""TGOS 全國門牌地址定位 provider（2026-08-22）。

為什麼加這條路：本專案的地址全部是台灣門牌，而 TGOS 整合的是戶政機關的門牌坐標資料 ——
對這批地址它是**權威來源**，不是「盡量給個接近答案」的通用地理編碼器。七-11 記著
OSM 會回「看起來正常但錯誤」的座標（模糊比對到別區的同名路），那個失效模式在
權威來源上不存在：查無此門牌就是查無。

**本檔不打真實 TGOS 服務**（需要政府機關申請的 AppID/APIKey）。守的是三件不需要憑證
也能驗、而且錯了會很貴的事：
  1. 沒有憑證時**絕不發出任何 HTTP request**（同 GEO_GOOGLE_ENABLED 的硬止血語意）。
  2. 送出的參數鎖住縣市與鄉鎮 —— 這是**安全性設定**，直接消滅七-11 的跨行政區誤配。
  3. 回應解析：X=經度 / Y=緯度不可弄反（弄反就是五-X 的翻版：地圖上看起來完全正常），
     且投影座標必須拒收而非硬當經緯度用。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")

import pytest

import app.services.geocode as geo


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("TGOS_APP_ID", "app-id-for-test")
    monkeypatch.setenv("TGOS_API_KEY", "api-key-for-test")
    monkeypatch.delenv("GEO_TGOS_ENABLED", raising=False)


# ── 啟用條件 ────────────────────────────────────────────────
def test_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("TGOS_APP_ID", raising=False)
    monkeypatch.delenv("TGOS_API_KEY", raising=False)
    assert geo._tgos_enabled() is False


@pytest.mark.parametrize("appid,apikey", [("x", ""), ("", "y"), ("", "")])
def test_needs_both_credentials(monkeypatch, appid, apikey):
    """只有一半憑證等於沒有憑證 —— 半套設定不該送出必然失敗的請求。"""
    monkeypatch.setenv("TGOS_APP_ID", appid)
    monkeypatch.setenv("TGOS_API_KEY", apikey)
    assert geo._tgos_enabled() is False


@pytest.mark.parametrize("off", ["0", "false", "NO", " Off "])
def test_explicit_kill_switch(monkeypatch, creds, off):
    monkeypatch.setenv("GEO_TGOS_ENABLED", off)
    assert geo._tgos_enabled() is False


def test_no_http_request_without_credentials(monkeypatch):
    """硬止血：沒憑證時連 request 都不建立（不是送出去被拒絕）。"""
    monkeypatch.delenv("TGOS_APP_ID", raising=False)
    monkeypatch.delenv("TGOS_API_KEY", raising=False)
    monkeypatch.setattr(geo.requests, "get",
                        lambda *a, **k: pytest.fail("無憑證時不該發出任何 HTTP request"))
    assert geo._tgos_geocode("高雄市苓雅區四維三路2號") is None


# ── 送出的參數（安全性設定）─────────────────────────────────
def test_request_locks_county_and_town(monkeypatch, creds):
    """鎖縣市 + 鄉鎮：讓模糊比對不得跨行政區。

    七-11 的實例是「高雄市**鳳山區**…建國路三段539號」被比到**路竹區**的建國路，
    偏差 26 公里而地圖上完全看不出來。鎖定行政區把那條路直接關掉：寧可查無，不可查錯。
    """
    seen = {}

    class _R:
        text = json.dumps({"AddressList": [{"X": 120.3, "Y": 22.6}]})
        def raise_for_status(self): pass

    def _get(url, params=None, timeout=None):
        seen["url"] = url; seen["params"] = params
        return _R()

    monkeypatch.setattr(geo.requests, "get", _get)
    assert geo._tgos_geocode("高雄市鳳山區建國路三段539號") == (22.6, 120.3)
    p = seen["params"]
    assert p["oIsLockCounty"] == "true"
    assert p["oIsLockTown"] == "true"
    assert p["oSRS"] == "EPSG:4326", "本專案全線 WGS84，不做座標轉換"
    assert p["oReturnMaxCount"] == "1", "多筆候選代表地址歧義，不該由程式挑"
    assert p["oAddress"] == "高雄市鳳山區建國路三段539號"
    # 憑證要真的帶上，而且不可寫死
    assert p["oAPPId"] == "app-id-for-test" and p["oAPIKey"] == "api-key-for-test"


def test_uses_v30_endpoint_by_default(monkeypatch):
    monkeypatch.delenv("TGOS_URL", raising=False)
    assert "addr.tgos.tw" in geo._tgos_url() and "v30" in geo._tgos_url()


def test_dead_legacy_url_is_ignored(monkeypatch):
    """專案 `.env` 沿用的舊端點已失效（2026-08-22 實測 404）→ 視為未設定。

    若照單全收，使用者好不容易申請到憑證後只會看到「TGOS 沒反應」，
    然後把時間花在懷疑金鑰上 —— 這種「設定看起來有值、實際必然失敗」的狀況
    要在程式裡擋掉，不能只寫在文件裡。
    """
    monkeypatch.setenv("TGOS_URL", "https://map.tgos.tw/TGOS/geocode/QueryAddr")
    assert geo._tgos_url() == geo._TGOS_URL_DEFAULT


def test_custom_url_is_respected(monkeypatch):
    """只擋已知失效的那一個值，不阻止使用者指定其他自訂端點。"""
    monkeypatch.setenv("TGOS_URL", "https://example.internal/geocode")
    assert geo._tgos_url() == "https://example.internal/geocode"


def test_network_error_returns_none(monkeypatch, creds):
    """外部服務掛掉不可讓整批 ingest 炸掉 —— 與 Google / OSM 同樣一律回 None。"""
    def _boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(geo.requests, "get", _boom)
    assert geo._tgos_geocode("高雄市苓雅區四維三路2號") is None


# ── 回應解析 ────────────────────────────────────────────────
def test_parse_plain_json():
    assert geo._tgos_parse(json.dumps({"AddressList": [{"X": 120.3014, "Y": 22.6273}]})) \
        == (22.6273, 120.3014)


def test_parse_asmx_string_wrapper():
    """.asmx 端點會把 JSON 包在一層 <string> 裡（ASP.NET WebService 的老行為）。"""
    body = '<?xml version="1.0"?><string>{"Addresses":[{"x":120.3,"y":22.6}]}</string>'
    assert geo._tgos_parse(body) == (22.6, 120.3)


def test_parse_is_case_insensitive_and_nested():
    """不同版本欄位大小寫與巢狀層級有出入，故遞迴尋找第一個帶 X/Y 的物件。"""
    body = json.dumps({"a": {"b": [{"noise": 1}, {"Y": 25.0, "X": 121.5}]}})
    assert geo._tgos_parse(body) == (25.0, 121.5)


def test_x_is_longitude_y_is_latitude():
    """X=經度、Y=緯度。弄反就是五-X 的翻版：地圖照樣畫得出漂亮但錯誤的點位。

    用一個台灣座標，經緯度互換後緯度會變成 120（超出 [-90,90]），
    所以「有沒有弄反」在這個斷言下藏不住。
    """
    lat, lng = geo._tgos_parse(json.dumps({"r": [{"X": 120.3014, "Y": 22.6273}]}))
    assert 21.5 <= lat <= 25.5, f"lat={lat} 不在台灣緯度範圍 —— 經緯度可能弄反"
    assert 119.5 <= lng <= 122.5, f"lng={lng} 不在台灣經度範圍"


def test_projected_coordinates_rejected_not_converted():
    """若回的是 TWD97 投影座標（公尺，值以十萬計）→ **拒收**。

    刻意不換算：換算需要知道是哪個帶，猜錯一樣得到錯的座標，
    而錯的座標在地圖上看不出來（七-11 的核心教訓）。
    """
    assert geo._tgos_parse(json.dumps({"r": [{"X": 185000.0, "Y": 2504000.0}]})) is None


@pytest.mark.parametrize("body", ["", "not json", "<string>oops</string>",
                                  json.dumps({"AddressList": []}),
                                  json.dumps({"r": [{"X": "abc", "Y": "def"}]})])
def test_parse_bad_input_returns_none(body):
    assert geo._tgos_parse(body) is None


# ── 在查詢鏈中的位置 ────────────────────────────────────────
def test_tgos_runs_before_google_and_osm(monkeypatch, creds):
    """權威來源優先、通用地理編碼器殿後。TGOS 命中就不該再問 Google / OSM。"""
    monkeypatch.setattr(geo, "_lookup_from_local", lambda cid, ad: None)
    monkeypatch.setattr(geo, "_cache_get", lambda a: None)
    monkeypatch.setattr(geo, "_cache_set", lambda *a: None)
    monkeypatch.setattr(geo, "_sql_cache_get_bulk", lambda addrs: {})
    monkeypatch.setattr(geo, "_sql_cache_set_bulk", lambda items: None)
    monkeypatch.setattr(geo, "_tgos_geocode", lambda a: (22.6, 120.3))
    monkeypatch.setattr(geo, "_google_geocode",
                        lambda a: pytest.fail("TGOS 命中後不該再打 Google"))
    monkeypatch.setattr(geo, "_osm_geocode",
                        lambda a: pytest.fail("TGOS 命中後不該再打 OSM"))
    assert geo.lookup(None, "高雄市苓雅區四維三路2號") == (22.6, 120.3)


def test_bulk_skips_google_for_addresses_tgos_resolved(monkeypatch, creds, capsys):
    """bulk 路徑：TGOS 解出來的地址不可再送進 Google 並行階段（重複打外部服務且浪費配額）。"""
    monkeypatch.setattr(geo, "_sql_cache_get_bulk", lambda addrs: {})
    monkeypatch.setattr(geo, "_sql_cache_set_bulk", lambda items: None)
    monkeypatch.setattr(geo, "_lookup_from_local_bulk", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(geo, "_google_enabled", lambda: True)
    monkeypatch.setattr(geo, "USE_OSM", False)
    resolved = {"台北市A路1號"}
    monkeypatch.setattr(geo, "_tgos_geocode",
                        lambda a: (25.0, 121.5) if a in resolved else None)
    google_seen = []
    monkeypatch.setattr(geo, "_google_geocode",
                        lambda a: google_seen.append(a) or None)

    geo.lookup_bulk([(None, "台北市A路1號"), (None, "台北市B路2號")])
    assert google_seen == ["台北市B路2號"], f"Google 只該收到 TGOS 未解出的那筆，實得 {google_seen}"
    line = [l for l in capsys.readouterr().out.splitlines() if "[bulk_geocode][timing]" in l]
    assert line and "tgos_calls=2" in line[0] and "google_calls=1" in line[0], line
