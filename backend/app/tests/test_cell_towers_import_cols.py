# backend/app/tests/test_cell_towers_import_cols.py
"""
cell_towers 匯入：欄位索引推導 + 座標範圍把關（2026-07-21）

Background
==========
`api/cell_towers.py` 原本用 `or` 鏈推導欄位索引：

    idx_lat = col.get("lat") or col.get("latitude") or 1

Python 的 **0 是 falsy**，所以欄位剛好排在第一欄時會被誤判為「找不到」而落到
位置後備值。實測後果：

  header `lat,lng,cell_id`  → idx_lat 與 idx_lng **同時指向第 1 欄**
                              → 每座基地台的緯度被寫成經度值
  header `lng,lat,cell_id`  → idx_lng 指到 cell_id 欄
                              → float(cell_id) 拋 ValueError，整批列被跳過

而原始碼**沒有任何座標範圍驗證**，所以第一種情況會**靜默寫入錯誤座標**。

為什麼這比一般的欄位對應 bug 嚴重：`cell_towers` 是 geocode 的最前置查詢
（`_lookup_from_local` 命中就直接採用、不再問 Google/OSM），而基地台座標**本身
就是證據**。寫錯之後地圖照樣畫得出漂亮的點位，整條軌跡卻是錯的，事後幾乎無從
察覺。業者交付的 CSV 欄序不是我方能控制的，不能假設「大家都把 cell_id 放第一欄」。

此 bug 在發現時尚未造成實害，因為 `cell_towers` 表一直是空的（待辦 #1 未執行）；
它會在「第一次匯入業者對照表」時觸發 —— 正好是這張表唯一會被寫入的時機。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


def _resolve(header: list[str]) -> tuple[int, int, int]:
    """重現 import_csv 的欄位索引推導（header 存在時的分支）。"""
    from app.api.cell_towers import _pick_col_req

    first = [c.strip().lower() for c in header]
    col = {name: i for i, name in enumerate(first)}
    return (
        _pick_col_req(col, "cell_id", "cell", default=0),
        _pick_col_req(col, "lat", "latitude", default=1),
        _pick_col_req(col, "lng", "longitude", default=2),
    )


# ─────────────────────────────────────────────────────────────
# 1. 欄位索引推導：各種欄序都要正確（含索引 0 的關鍵情境）
# ─────────────────────────────────────────────────────────────
def test_col_index_canonical_order():
    assert _resolve(["cell_id", "lat", "lng"]) == (0, 1, 2)


def test_col_index_lat_lng_swapped_in_header():
    """欄名有寫、但經緯度欄對調 → 索引必須跟著對調。"""
    assert _resolve(["cell_id", "lng", "lat"]) == (0, 2, 1)


def test_col_index_lat_at_position_zero():
    """
    迴歸核心：lat 在第一欄（索引 0）。
    舊版 `or` 鏈會把 0 當 falsy → idx_lat 落到 1、與 idx_lng 撞在同一欄
    → 緯度被寫成經度值。
    """
    assert _resolve(["lat", "lng", "cell_id"]) == (2, 0, 1)


def test_col_index_latitude_longitude_at_position_zero():
    """同上，但用完整欄名 latitude/longitude。"""
    assert _resolve(["latitude", "longitude", "cell_id"]) == (2, 0, 1)


def test_col_index_lng_at_position_zero():
    """
    lng 在第一欄。舊版 idx_lng 會落到 2（cell_id 欄）
    → float(cell_id) ValueError → 整批列以「格式錯誤」被跳過。
    """
    assert _resolve(["lng", "lat", "cell_id"]) == (2, 1, 0)


def test_col_index_alias_forms():
    """cell/latitude/longitude 別名亦須正確解析。"""
    assert _resolve(["cell", "latitude", "longitude"]) == (0, 1, 2)


def test_col_index_falls_back_to_positions_when_unknown():
    """完全不認識的欄名 → 落回位置預設 (0,1,2)，維持既有向後相容行為。"""
    assert _resolve(["編號", "X", "Y"]) == (0, 1, 2)


def test_pick_col_returns_zero_not_default():
    """
    直接鎖住 helper 本身的語意：索引 0 必須被視為「找到了」。
    這條是整個 bug 的根，值得獨立一條而不只靠上面的組合測試。
    """
    from app.api.cell_towers import _pick_col, _pick_col_req

    assert _pick_col({"lat": 0}, "lat") == 0
    assert _pick_col_req({"lat": 0}, "lat", default=99) == 0
    assert _pick_col({"other": 1}, "lat") is None
    assert _pick_col_req({"other": 1}, "lat", default=99) == 99


# ─────────────────────────────────────────────────────────────
# 2. 座標範圍把關（falsy-zero 類錯誤的第二道防線）
# ─────────────────────────────────────────────────────────────
def _in_range(lat: float, lng: float) -> bool:
    from app.api.cell_towers import _LAT_MAX, _LAT_MIN, _LNG_MAX, _LNG_MIN

    return (_LAT_MIN <= lat <= _LAT_MAX) and (_LNG_MIN <= lng <= _LNG_MAX)


def test_valid_taiwan_coordinate_accepted():
    assert _in_range(22.6273, 120.3014)


def test_swapped_taiwan_coordinate_rejected():
    """
    台灣經度（約 120~122）落在合法緯度範圍之外，故「經緯度對調」這個最常見的
    實務失誤會被範圍檢查攔下 —— 這正是 falsy-zero bug 的實際失效形態。
    """
    assert not _in_range(120.3014, 22.6273)


def test_out_of_range_rejected():
    assert not _in_range(91.0, 120.0)
    assert not _in_range(22.0, 181.0)
    assert not _in_range(-90.1, 120.0)


def test_range_boundaries_accepted():
    """邊界值屬合法（-90/90/-180/180）。"""
    assert _in_range(90.0, 180.0)
    assert _in_range(-90.0, -180.0)
