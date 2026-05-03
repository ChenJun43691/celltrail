# backend/app/tests/test_ingest_dialect_detection.py
"""
W2.4 方言偵測 + dialect-aware normalize 測試（2026-04-29）

驗證 _detect_dialect + _normalize_row(dialect=...) 在中華上網方言下的
正確行為，並確保標準方言（W1.5/W2.2/W2.3）零回歸。

Cases：
  A. _detect_dialect 純函式（6 cases）
     1. 標準命中：headers 含「起台 + 起址」+ 通話類別含「上網」
     2. 不命中：缺「起台」（headers 訊號 A 失敗）
     3. 不命中：通話類別不含「上網」（訊號 B 失敗）
     4. 不命中：sample_rows 全空（訊號 B 無法判斷 → 保守拒絕）
     5. 邊界：通話類別「中華上網」與「亞太上網」混合 → 命中
     6. 邊界：通話類別「上網」< 50% → 不命中
  B. _normalize_row(dialect="cht_internet") 整合（4 cases）
     7. 中華上網方言基本路徑：起台→start_ts、起址→cell_id、通話對象→cell_addr
     8. 方言下無意義欄位被跳過（IMEI、備考、編號、迄台/迄址）
     9. dialect 從 raw row 的 __celltrail_dialect__ tag 自動偵測（介面對舊呼叫透明）
    10. dialect 內空值不覆蓋（與 W1.5 一致）
  C. W1.5 / W2.3 零回歸（2 spot check）
    11. dialect=None → W1.5「後者覆蓋」維持
    12. dialect=None → W2.3 複合欄拆解維持

不依賴 DB；用 monkeypatch 強制走 _RAW2CANON fallback。
"""
from __future__ import annotations

import os

# 必須在 import app.* 之前設好環境變數
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


def _patch_active_map_to_default(monkeypatch):
    import app.services.carrier_profile as cp
    monkeypatch.setattr(cp, "_load_default_profile_from_db", lambda: None)
    cp.invalidate_cache()


# ─────────────────────────────────────────────────────────────
# A. _detect_dialect 純函式
# ─────────────────────────────────────────────────────────────
def test_detect_dialect_standard_hit():
    """周蔓達實測 schema：headers 含「起台+起址」+ row 通話類別=中華上網"""
    from app.services.ingest import _detect_dialect

    headers = ["編號", "起台", "起址", "迄台", "迄址", "通話類別", "通話對象"]
    sample_rows = [
        {"通話類別": "中華上網", "起台": "2023-01-12T00:48:02.000", "起址": "13792"},
        {"通話類別": "中華上網", "起台": "2023-01-12T00:48:03.000", "起址": "13792"},
    ]
    assert _detect_dialect(headers, sample_rows) == "cht_internet"


def test_detect_dialect_missing_header_signal():
    """訊號 A 失敗：缺「起台」這對指紋欄之一 → 不命中（即使 row 是中華上網）"""
    from app.services.ingest import _detect_dialect

    headers = ["編號", "起址", "通話類別"]  # 沒有「起台」
    sample_rows = [{"通話類別": "中華上網"}]
    assert _detect_dialect(headers, sample_rows) is None


def test_detect_dialect_missing_internet_keyword():
    """訊號 B 失敗：通話類別不含「上網」 → 不命中"""
    from app.services.ingest import _detect_dialect

    headers = ["起台", "起址", "通話類別"]
    sample_rows = [
        {"通話類別": "市話"},
        {"通話類別": "行動電話"},
    ]
    assert _detect_dialect(headers, sample_rows) is None


def test_detect_dialect_no_categorized_rows():
    """sample_rows 沒有任何「通話類別」非空 → 保守拒絕（None）"""
    from app.services.ingest import _detect_dialect

    headers = ["起台", "起址", "通話類別"]
    sample_rows = [
        {"通話類別": ""},
        {"通話類別": None},
        {},  # 連 key 都沒有
    ]
    assert _detect_dialect(headers, sample_rows) is None


def test_detect_dialect_mixed_internet_carriers():
    """邊界：通話類別 100% 是「上網」但跨 carrier（中華+亞太+遠傳）→ 仍命中"""
    from app.services.ingest import _detect_dialect

    headers = ["起台", "起址", "通話類別"]
    sample_rows = [
        {"通話類別": "中華上網"},
        {"通話類別": "亞太上網"},
        {"通話類別": "遠傳上網"},
    ]
    # 訊號 B 是「含上網」≥50%，跨 carrier 不影響命中
    # 設計理由：方言偵測的關鍵是「上網類事件 schema」而非「特定 carrier」
    assert _detect_dialect(headers, sample_rows) == "cht_internet"


def test_detect_dialect_below_threshold():
    """邊界：通話類別含「上網」< 50% → 不命中（避免誤觸混合 sheet）"""
    from app.services.ingest import _detect_dialect

    headers = ["起台", "起址", "通話類別"]
    sample_rows = [
        {"通話類別": "中華上網"},   # 1/4 = 25% < 50%
        {"通話類別": "市話"},
        {"通話類別": "市話"},
        {"通話類別": "行動電話"},
    ]
    assert _detect_dialect(headers, sample_rows) is None


# ─────────────────────────────────────────────────────────────
# B. _normalize_row(dialect="cht_internet") 整合
# ─────────────────────────────────────────────────────────────
def test_normalize_dialect_cht_internet_core_mapping(monkeypatch):
    """中華上網方言核心三欄：起台→start_ts、起址→cell_id、通話對象→cell_addr"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    raw = {
        "編號": "N0001",
        "起台": "2023-01-12T00:48:02.000",
        "起址": "13792",
        "通話對象": "臺北市士林區承德路4段166號樓頂機房",
        "通話類別": "中華上網",
    }
    out = _normalize_row(raw, dialect="cht_internet")
    assert out.get("start_ts") == "2023-01-12T00:48:02.000"
    assert out.get("cell_id") == "13792"
    assert out.get("cell_addr") == "臺北市士林區承德路4段166號樓頂機房"
    # 確認「編號」「通話類別」沒被誤對應到 canonical key
    assert "編號" not in out
    assert "通話類別" not in out


def test_normalize_dialect_skips_irrelevant_columns(monkeypatch):
    """方言下無意義的欄位（IMEI、備考、迄台、迄址、始話日期/時間）都被明確跳過"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    raw = {
        "起台": "2023-01-12T00:48:02.000",
        "起址": "13792",
        "通話對象": "臺北市某地址",
        # 以下都該被 dialect 路徑跳過
        "IMEI": "359663601479290",
        "備考": "85-3544-2L3EPG3_PGW",
        "編號": "N0001",
        "申設人": "申設人",
        "秒數": "0",
        "始話日期": "2023-01-12",
        "始話時間": "00:48:02",
        "迄台": "should_be_ignored",
        "迄址": "should_be_ignored",
        "轉接電話": "手機連到基地台",
    }
    out = _normalize_row(raw, dialect="cht_internet")
    # 只該有方言核心三欄
    assert set(out.keys()) == {"start_ts", "cell_id", "cell_addr"}


def test_normalize_dialect_auto_pop_from_tag(monkeypatch):
    """
    dialect 從 raw row 的 __celltrail_dialect__ tag 自動偵測（介面對舊呼叫透明）
    這是 _iter_rows_excel 注入 tag → _ingest_rows_stream 不需改 → _normalize_row
    自動消化的關鍵設計：既有 `_normalize_row(raw)` 呼叫端零異動。
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    raw = {
        "起台": "2023-01-12T00:48:02.000",
        "起址": "13792",
        "通話對象": "臺北市某地址",
        "__celltrail_dialect__": "cht_internet",
    }
    # 故意不傳 dialect 參數
    out = _normalize_row(raw)
    assert out.get("start_ts") == "2023-01-12T00:48:02.000"
    assert out.get("cell_id") == "13792"
    assert out.get("cell_addr") == "臺北市某地址"
    # tag 不該外洩到 out
    assert "__celltrail_dialect__" not in out


def test_normalize_dialect_empty_value_not_overwrite(monkeypatch):
    """
    dialect 路徑的空值處理（與 W1.5 一致原則）：空字串/純空白/None 都跳過。
    避免單一 raw key 在不同 row 出現空值時破壞已寫入的 row。
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    raw = {
        "起台": "2023-01-12T00:48:02.000",
        "起址": "",          # 空字串 → 跳過
        "通話對象": "   ",   # 純空白 → 跳過
    }
    out = _normalize_row(raw, dialect="cht_internet")
    assert out.get("start_ts") == "2023-01-12T00:48:02.000"
    assert "cell_id" not in out
    assert "cell_addr" not in out


# ─────────────────────────────────────────────────────────────
# C. W1.5 / W2.3 零回歸 spot check
# ─────────────────────────────────────────────────────────────
def test_no_regression_w1_5_later_overwrite(monkeypatch):
    """dialect=None → W1.5「多源後者覆蓋」維持（與 W1.5 case 4 對應）"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({"基地台ID": "FIRST", "最終基地台ID": "FINAL"})
    assert out.get("cell_id") == "FINAL"


def test_no_regression_w2_3_compound_split(monkeypatch):
    """dialect=None → W2.3 複合欄拆解維持（與 W2.3 [B1] 對應）"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "迄基地台": "46601493130200051012 新北市中和區1號(4G)",
    })
    assert out.get("cell_id") == "46601493130200051012"
    assert out.get("cell_addr") == "新北市中和區1號(4G)"


# ─────────────────────────────────────────────────────────────
# D. _parse_ts ISO 8601 補強（W2.4-pre）
# ─────────────────────────────────────────────────────────────
def test_parse_ts_iso8601_with_millisecond():
    """中華上網方言 100% 格式：'YYYY-MM-DDTHH:MM:SS.fff'"""
    from app.services.ingest import _parse_ts

    dt = _parse_ts("2023-01-12T00:48:02.000")
    assert dt is not None
    assert dt.year == 2023 and dt.month == 1 and dt.day == 12
    assert dt.hour == 0 and dt.minute == 48 and dt.second == 2
    # 必須帶台北時區
    assert dt.utcoffset().total_seconds() == 8 * 3600


def test_parse_ts_iso8601_without_millisecond():
    """ISO 8601 無毫秒備援"""
    from app.services.ingest import _parse_ts

    dt = _parse_ts("2024-12-31T23:59:59")
    assert dt is not None
    assert dt.year == 2024 and dt.month == 12 and dt.day == 31


def test_parse_ts_iso8601_rejects_timezone_suffix():
    """
    刻意拒絕 'Z' 與 '+08:00' 後綴：避免時區雙重標記歧義。
    我們的時間欄位假設都是 naïve 台北時間、由 _parse_ts 統一加 TPE_TZ。
    若原始欄已標時區，應該由更上層的 dialect handler 處理、而非 _parse_ts。
    """
    from app.services.ingest import _parse_ts

    assert _parse_ts("2023-01-12T00:48:02Z") is None
    assert _parse_ts("2023-01-12T00:48:02+08:00") is None
