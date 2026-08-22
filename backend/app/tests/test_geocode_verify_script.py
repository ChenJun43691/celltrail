# backend/app/tests/test_geocode_verify_script.py
"""
scripts/geocode_verify.py 的純邏輯守門（2026-07-22）

本檔只測**不需網路**的判準函式 —— 但它們正是決定「一筆推估座標會不會被採用」
的關鍵。判斷錯了，錯誤的基地台座標就會流進 cell_towers，而錯誤座標在地圖上
與正確者完全無法分辨（CLAUDE.md 七-11）。

各函式的實測背景：
  strip_village      —— 業者把「里/鄰」寫進地址欄，Nominatim 幾乎一律查無；
                        剝除後命中率大幅提升。但字元類別若沒排除行政層級用字，
                        會貪婪吃掉「區」而破壞地址（實測踩過的雷）。
  admin_of / road_of —— 驗證的比對基準。取錯就等於沒驗證。
  roads_compatible   —— 反查常回更細的層級（「大豐一路288巷」vs「大豐一路」），
                        那是同一條路，不能判為不符。
"""
from __future__ import annotations

import importlib.util
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "geocode_verify.py",
)


@pytest.fixture(scope="module")
def gv():
    spec = importlib.util.spec_from_file_location("geocode_verify", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── strip_village ────────────────────────────────────────────
def test_strip_village_keeps_district(gv):
    """
    迴歸核心：剝除「里」時不得吃掉前一級的「區」。
    錯誤版本 `[一-鿿]{1,3}里` 會把「區文福里」整段吃掉 → 「高雄市鳳山建國路…」
    （少了「區」），地址結構被破壞、查詢必然失敗。
    """
    assert gv.strip_village("高雄市鳳山區文福里建國路三段539號") == "高雄市鳳山區建國路三段539號"
    assert gv.strip_village("高雄市三民區寶業里陽明路170巷8號") == "高雄市三民區陽明路170巷8號"


def test_strip_village_removes_numeric_neighborhood(gv):
    """「15鄰」是純數字鄰別，無歧義，一律移除。"""
    assert gv.strip_village("高雄市田寮區崇德路15鄰山頂巷20-1號") == "高雄市田寮區崇德路山頂巷20-1號"


def test_strip_village_noop_when_no_village(gv):
    assert gv.strip_village("高雄市前金區中正四路211號") == "高雄市前金區中正四路211號"


def test_strip_village_keeps_original_when_structure_would_break(gv):
    """
    剝除後若已不具地址結構（無路/街/段/巷/弄），寧可保留原式 ——
    寧可查不到，也不要送出一個殘缺字串去換模糊比對的結果。
    """
    a = "台南市楠西區龜丹里龜丹59之6號"
    assert "龜丹" in gv.strip_village(a)


# ── admin_of / road_of ───────────────────────────────────────
def test_admin_of_extracts_city_and_district(gv):
    assert gv.admin_of("高雄市鳳山區文福里建國路三段539號") == ("高雄市", "鳳山區")
    assert gv.admin_of("苗栗縣西湖鄉湖東村8鄰埔頂31號") == ("苗栗縣", "西湖鄉")


def test_admin_of_returns_none_when_unparseable(gv):
    """取不到行政區 → 呼叫端必須視為「無法驗證」而拒絕採用。"""
    assert gv.admin_of("某某段123地號") == (None, None)
    assert gv.admin_of("") == (None, None)


def test_road_of_excludes_administrative_tokens(gv):
    """
    路名擷取必須排除區/鄉/鎮/里/鄰字元，否則會把行政區名吃進路名，
    導致比對基準本身就是錯的。
    """
    assert gv.road_of("高雄市三民區寶業里陽明路170巷8號") == "陽明路"
    assert gv.road_of("高雄市鳳山區文福里建國路三段539號") == "建國路"


def test_road_of_none_for_parcel_address(gv):
    """地號型地址無路名 → None，呼叫端據此拒絕（無從驗證）。"""
    assert gv.road_of("高雄市路竹區營後里營後段129地號") is None


# ── roads_compatible ─────────────────────────────────────────
def test_roads_compatible_accepts_finer_granularity(gv):
    """反查回更細層級屬同一條路，不可判為不符（實測案例）。"""
    assert gv.roads_compatible("大豐一路", "大豐一路288巷")
    assert gv.roads_compatible("義華路", "義華路272巷")
    assert gv.roads_compatible("陽明路", "陽明路")


def test_roads_compatible_rejects_different_roads(gv):
    """
    這幾組都是實測中「行政區相符但路名錯」的真實案例 ——
    只做行政區驗證會放行，必須靠路名驗證擋下。
    """
    assert not gv.roads_compatible("皓東路", "春陽街184巷")
    assert not gv.roads_compatible("覺民路", "民壯路")
    assert not gv.roads_compatible("自強三路", "永興街")
    assert not gv.roads_compatible("澄清路", "澄和路15巷")


def test_roads_compatible_rejects_missing_side(gv):
    """任一側取不到路名即無從驗證 → 一律不採用（寧可少，不可錯）。"""
    assert not gv.roads_compatible(None, "陽明路")
    assert not gv.roads_compatible("陽明路", None)
    assert not gv.roads_compatible("陽明路", "")


# ── NLSC 官方行政區反查驗證器（2026-08-22）────────────────────
def test_nlsc_ssl_context_keeps_verification_on(gv):
    """NLSC 憑證缺 Subject Key Identifier，Python 3.13 的 VERIFY_X509_STRICT 會拒連。

    只關 strict 旗標，**憑證鏈與主機名驗證必須保留**：反查結果正是我們判斷
    「這個座標可不可信」的依據，驗證器本身被中間人騙了，整套驗證就失去意義（七-11）。
    這條測試存在的意義是防止有人日後圖方便改成 `ssl._create_unverified_context()`。
    """
    import ssl
    ctx = gv._nlsc_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED, "不得關閉憑證鏈驗證"
    assert ctx.check_hostname is True, "不得關閉主機名檢查"
    assert not (ctx.verify_flags & ssl.VERIFY_X509_STRICT), "須關閉 strict 旗標才連得上 NLSC"


def test_nlsc_reverse_parses_city_and_town(gv, monkeypatch):
    """回的是 XML（非 JSON）；取縣市、鄉鎮與村里。

    村里在 2026-08-22 加入：業者地址有 20.8% 帶里名（涵蓋 51.4% 的列），
    而里比區細得多，是能分辨「同一區內定位到錯誤街廓」的唯一免費訊號。
    但它**不是預設的拒絕條件**（里界附近反查到相鄰里屬正常），只在報告中揭露。
    """
    body = ("<?xml version='1.0'?><townVillageItem><ctyCode>E</ctyCode>"
            "<ctyName>高雄市</ctyName><townName>新興區</townName>"
            "<villageName>成功里</villageName></townVillageItem>")

    class _R:
        status = 200
        def read(self): return body.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(gv.urllib.request, "urlopen", lambda *a, **k: _R())
    monkeypatch.setattr(gv.time, "sleep", lambda s: None)
    assert gv.reverse_nlsc(22.6273, 120.3014) == ("高雄市", "新興區", "成功里")


def test_nlsc_reverse_network_failure_is_not_fatal(gv, monkeypatch):
    """反查失敗回 (None, None) → 上層判定為「無法驗證」而拒收該址。

    關鍵是**拒收而非放行**：驗證器掛掉時放行等於沒有驗證，會讓未經確認的座標流進證據。
    """
    def _boom(*a, **k): raise OSError("connection reset")
    monkeypatch.setattr(gv.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(gv.time, "sleep", lambda s: None)
    assert gv.reverse_nlsc(22.6, 120.3) == (None, None, None)


def test_nlsc_reverse_throttles_even_on_failure(gv, monkeypatch):
    """節流必須對每次請求生效（含失敗）—— 與 geocode.py 同款修正（commit f99ea5b）。"""
    slept = []
    monkeypatch.setattr(gv.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(gv.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    gv.reverse_nlsc(22.6, 120.3)
    assert slept, "失敗路徑也必須節流，否則錯誤會變成對官方 API 的洪水"


# ── memo 是證據來源標註，必須如實 ──────────────────────────
def test_memo_reflects_both_provider_and_verifier(gv):
    """memo 會逐列寫進 cell_towers 並在稽核時被讀 —— 它必須同時如實反映
    「座標哪裡來」與「用什麼驗過」。

    這條測試源自一個實際犯過的錯：memo 曾寫死成 OSM 版，於是 `--verifier nlsc`
    跑出來的 CSV 仍宣稱「路名雙重反查驗證(OSM)」—— 而 NLSC 那條路**根本不比對路名**。
    對稽核者說不實的話，比不說更糟。
    """
    m = gv.build_memo("google", "nlsc")
    assert "Google" in m and "NLSC" in m
    assert "OSM" not in m, "沒用到 OSM 就不可宣稱經 OSM 驗證"
    assert "路名" not in m, "NLSC 驗證器不比對路名，不可宣稱做過"

    m2 = gv.build_memo("osm", "osm")
    assert "OSM" in m2 and "路名" in m2

    m3 = gv.build_memo("tgos", "nlsc")
    assert "TGOS" in m3 and "NLSC" in m3


def test_memo_always_marks_non_carrier_origin(gv):
    """所有組合都必須標明「非業者提供」——這批座標全是推估/第三方比對的結果，
    與業者登記的站台座標在證據上的份量不同，不可讓兩者在表裡看起來一樣。"""
    for p in ("osm", "google", "tgos"):
        for v in ("osm", "nlsc"):
            assert "非業者提供" in gv.build_memo(p, v)


# ── village_of：里名擷取（2026-08-22）────────────────────────
@pytest.mark.parametrize("addr,want", [
    ("高雄市鳳山區文福里建國路三段539號11樓室內(4G)", "文福里"),
    ("高雄市三民區本館里昌裕街1號7樓樓頂(4G)", "本館里"),
    ("高雄市三民區寶國里1鄰覺民路168號樓頂(4G)", "寶國里"),
    ("臺北市士林區承德路4段166號樓頂機房", None),        # 沒有里
    ("高雄市湖內區中山路２段290號", None),
])
def test_village_of(gv, addr, want):
    assert gv.village_of(addr) == want


def test_village_of_does_not_match_road_containing_li(gv):
    """『里港路』的「里」不是里名 —— 只認「區/鄉/鎮之後緊接的里」。

    誤判的代價是拿一個不存在的里去比對反查結果，把正確座標標成可疑，
    讓人把注意力花在假警報上。
    """
    assert gv.village_of("屏東縣里港鄉里港路100號") is None
    assert gv.village_of("高雄市三民區里港街5號") is None


def test_village_of_handles_empty(gv):
    assert gv.village_of("") is None and gv.village_of(None) is None


# ── village_verdict：里名判定（2026-08-22）──────────────────
def test_village_verdict_ok_and_mismatch(gv):
    a = "高雄市鳳山區文福里建國路三段539號"
    assert gv.village_verdict(a, "文福里") == "ok"
    assert gv.village_verdict(a, "文德里") == "mismatch"


def test_village_verdict_unknown_is_not_ok(gv):
    """無從判斷必須是 'unknown'，不可當成 'ok'。

    本專案 79.2% 的地址沒有里名。若把「沒得比」算成「比對通過」，
    報告會顯示「全部驗過了」，而實際上有一大半根本沒驗到里這一層 ——
    那是對稽核者的誤導，比不驗更糟。
    """
    assert gv.village_verdict("高雄市湖內區中山路２段290號", "海埔里") == "unknown"
    assert gv.village_verdict("高雄市鳳山區文福里建國路三段539號", None) == "unknown"
    assert gv.village_verdict("", None) == "unknown"


def test_village_verdict_tolerates_whitespace(gv):
    assert gv.village_verdict("高雄市三民區本館里昌裕街1號", " 本館里 ") == "ok"
