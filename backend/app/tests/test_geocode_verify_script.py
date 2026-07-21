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
