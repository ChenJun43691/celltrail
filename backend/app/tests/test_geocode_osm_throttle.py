# backend/app/tests/test_geocode_osm_throttle.py
"""
OSM 節流必須對「每一次送出的請求」生效（2026-07-21）

Background
==========
`_osm_geocode` 原本把 `time.sleep(1.0)` 寫在 `if data:` 區塊**內部** ——
也就是**只有查到結果時才節流**。而未命中才是多數情況（台灣門牌在 OSM
覆蓋稀疏），於是請求連發、直接違反 Nominatim 的 1 req/s 使用政策。

實測後果（以本專案三個真實案件檔的地址抽樣）：
  - 大量 `429 Too many requests`，這些地址被記成「查無結果」
    → **命中率被系統性低估**（修正前 17.5% → 修正後 22.5%，且修正後前段
      仍受先前突發流量的殘留節流影響，實際更高）
  - 被 429 的請求還會拖長整批查詢時間

也就是說，這個 bug 同時製造了「OSM 沒用」與「OSM 很慢」兩個假象，
讓人誤判 OSM 備援不值得留。

本檔用假的 requests.get 與可觀測的 sleep，鎖住「命中與否都要節流」。
不打真網路、不碰 DB。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def osm(monkeypatch):
    """啟用 OSM、攔截網路與 sleep，回傳 (module, sleeps, requests) 記錄容器。"""
    import app.services.geocode as geo

    monkeypatch.setattr(geo, "USE_OSM", True)
    sleeps: list[float] = []
    reqs: list[dict] = []
    monkeypatch.setattr(geo.time, "sleep", lambda s: sleeps.append(s))
    return geo, sleeps, reqs


def _install(geo, monkeypatch, reqs, responder):
    def _fake_get(url, params=None, headers=None, timeout=None):
        reqs.append(dict(params or {}))
        return responder(params or {})

    monkeypatch.setattr(geo.requests, "get", _fake_get)


def test_throttles_on_miss(osm, monkeypatch):
    """
    查無結果時也必須節流 —— 這是修正前完全缺失的路徑，也是實務上最常走到的。
    未命中會走完 Pass 1（自由格式）+ Pass 2（結構化），故應有 2 次請求、2 次節流。
    """
    geo, sleeps, reqs = osm
    _install(geo, monkeypatch, reqs, lambda p: _Resp([]))     # 一律查無

    assert geo._osm_geocode("高雄市三民區某某路1號") is None
    assert len(reqs) == 2, f"未命中應走完兩段查詢，實得 {len(reqs)} 次請求"
    assert len(sleeps) == len(reqs), (
        f"每次請求都必須節流；請求 {len(reqs)} 次但只 sleep {len(sleeps)} 次"
    )
    assert all(s >= 1.0 for s in sleeps), "節流間隔不得小於 Nominatim 政策的 1 秒"


def test_throttles_on_hit(osm, monkeypatch):
    """命中時仍需節流（原行為，不得因修正而消失）。"""
    geo, sleeps, reqs = osm
    _install(geo, monkeypatch, reqs, lambda p: _Resp([{"lat": "22.6", "lon": "120.3"}]))

    assert geo._osm_geocode("高雄市前金區中正四路211號") == (22.6, 120.3)
    assert len(reqs) == 1, "Pass 1 命中就不該再打 Pass 2"
    assert len(sleeps) == 1 and sleeps[0] >= 1.0


def test_throttles_on_http_error(osm, monkeypatch):
    """
    HTTP 例外（含 429）時更必須節流 —— 否則會對著已在限流的服務繼續連發，
    把暫時性限流惡化成持續性封鎖。
    """
    geo, sleeps, reqs = osm
    _install(geo, monkeypatch, reqs, lambda p: _Resp([], status=429))

    assert geo._osm_geocode("高雄市鳳山區某某路2號") is None
    assert len(reqs) == 2
    assert len(sleeps) == 2, "429 之後沒有節流，等於持續攻擊對方服務"


def test_no_request_when_osm_disabled(monkeypatch):
    """
    GEO_OSM_FALLBACK 關閉時必須在送出任何請求前就返回（production 目前的組態）。
    這保證關閉開關能真正把 OSM 成本降為零，而不是照樣連線只是丟棄結果。
    """
    import app.services.geocode as geo

    monkeypatch.setattr(geo, "USE_OSM", False)
    called = []
    monkeypatch.setattr(geo.requests, "get",
                        lambda *a, **k: called.append(1) or _Resp([]))
    monkeypatch.setattr(geo.time, "sleep", lambda s: called.append("sleep"))

    assert geo._osm_geocode("高雄市三民區某某路3號") is None
    assert called == [], "OSM 停用時不得產生任何請求或延遲"
