# backend/app/tests/test_ingest_compound_split.py
"""
W2.3 複合欄拆解單元測試（2026-04-29）

驗證 _split_compound_cell + _normalize_row 在「迄基地台」/「終話基地台」
這類 cell_id_compound 欄上的拆解行為，並確保不破壞 W1.5 既有 fallback。

Cases：
  1. 標準格式：「ID 地址(代次)」拆對
  2. 無代次標籤：「ID 地址」拆對
  3. 單純空白 / 空字串 / None：拆出 (None, None)
  4. 只有 ID 段（無地址）：(cell_id, None)
  5. 只有地址（含中文、無 ID）：(None, cell_addr)
  6. 起欄已有值 + 迄欄複合：起欄不被覆蓋（W1.5 + W2.3 整合）
  7. 起迄都空：拆完直接填入

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
# A. _split_compound_cell 純函式行為
# ─────────────────────────────────────────────────────────────
def test_split_standard_with_tech_tag():
    """彭奕翔實測格式：'ID 地址(4G)' → 拆對，代次標籤保留在 cell_addr"""
    from app.services.ingest import _split_compound_cell

    cid, addr = _split_compound_cell(
        "46601493130200051012 新北市中和區泰安里景安路48號12樓頂樓(4G)"
    )
    assert cid == "46601493130200051012"
    assert addr == "新北市中和區泰安里景安路48號12樓頂樓(4G)"
    # 代次標籤刻意保留：forensic「保留原始」原則 + 將來分析可再 regex 抽出


def test_split_no_tech_tag():
    """無代次標籤：'ID 地址' → 拆對"""
    from app.services.ingest import _split_compound_cell

    cid, addr = _split_compound_cell("12345ABC 台北市中山區1號")
    assert cid == "12345ABC"
    assert addr == "台北市中山區1號"


def test_split_empty_returns_pair_of_none():
    from app.services.ingest import _split_compound_cell

    assert _split_compound_cell("") == (None, None)
    assert _split_compound_cell("    ") == (None, None)
    assert _split_compound_cell(None) == (None, None)
    assert _split_compound_cell("\t\n  ") == (None, None)


def test_split_id_only():
    """單一 token、不含中文 → 視為純 ID"""
    from app.services.ingest import _split_compound_cell

    cid, addr = _split_compound_cell("46601493130200051012")
    assert cid == "46601493130200051012"
    assert addr is None


def test_split_addr_only():
    """單一 token、含中文 → 視為純地址（罕見但保險）"""
    from app.services.ingest import _split_compound_cell

    cid, addr = _split_compound_cell("台北市中山區1號")
    assert cid is None
    assert addr == "台北市中山區1號"


def test_split_multiple_spaces():
    """多重空白應只切第一個分隔；地址內的空白保留"""
    from app.services.ingest import _split_compound_cell

    cid, addr = _split_compound_cell(
        "ABC123  Taipei City   Zhongshan District(LTE)"
    )
    assert cid == "ABC123"
    # 第一段空白被切掉，剩餘地址內的多重空白應保留（不要 collapse）
    assert addr == "Taipei City   Zhongshan District(LTE)"


# ─────────────────────────────────────────────────────────────
# B. _normalize_row 整合：W2.3 + W1.5 共存
# ─────────────────────────────────────────────────────────────
def test_normalize_compound_alone(monkeypatch):
    """只有「迄基地台」一個來源 → 拆解後填入 cell_id + cell_addr"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "迄基地台": "46601493130200051012 新北市中和區泰安里景安路48號12樓頂樓(4G)",
    })
    assert out.get("cell_id") == "46601493130200051012"
    assert out.get("cell_addr") == "新北市中和區泰安里景安路48號12樓頂樓(4G)"
    # cell_id_compound 不該以原 key 殘留在 out（已被拆完轉成 cell_id/cell_addr）
    assert "cell_id_compound" not in out


def test_normalize_compound_does_not_clobber_direct_cell_id(monkeypatch):
    """
    起欄「基地台ID」已有值 + 迄欄「迄基地台」也有值
    → 起欄保留，迄欄只補 cell_addr（fallback 角色）
    這是 W2.3 與 W1.5 整合的核心驗收：複合欄絕不蓋過原生欄。
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台ID": "ORIGINAL_ID",
        "迄基地台": "FALLBACK_ID 新北市中和區1號(4G)",
    })
    assert out.get("cell_id") == "ORIGINAL_ID", (
        "複合欄不該覆蓋原生 cell_id（W2.3 fallback 紀律）"
    )
    # cell_addr 在原 row 中沒有直接欄，所以可以由迄欄補入
    assert out.get("cell_addr") == "新北市中和區1號(4G)"


def test_normalize_compound_fills_only_empty_addr(monkeypatch):
    """
    cell_addr 已從「基地台位址」直接欄填入 + 迄欄複合也帶地址
    → cell_addr 保留直接欄值（複合欄不覆蓋）
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台ID": "DIRECT_ID",
        "基地台位址": "DIRECT_ADDR_直接欄值",
        "迄基地台": "FALLBACK_ID 迄欄拆出來的地址(4G)",
    })
    assert out.get("cell_id") == "DIRECT_ID"
    assert out.get("cell_addr") == "DIRECT_ADDR_直接欄值"


def test_normalize_compound_with_empty_direct(monkeypatch):
    """
    彭奕翔典型情境：起欄全空、迄欄複合
    → 走 W1.5 空值跳過 + W2.3 拆解填入
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台": " ",   # 起欄空白（W1.5 跳過）
        "迄基地台": "46601493130200051012 新北市中和區1號(4G)",
    })
    assert out.get("cell_id") == "46601493130200051012", (
        "起欄空白時，複合欄拆出的 ID 應填入"
    )
    assert out.get("cell_addr") == "新北市中和區1號(4G)"


def test_normalize_compound_alias_終話基地台(monkeypatch):
    """「終話基地台」別名也走複合欄路徑（同義別名應與「迄基地台」等效）"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "終話基地台": "ABC123 高雄市左營區1號",
    })
    assert out.get("cell_id") == "ABC123"
    assert out.get("cell_addr") == "高雄市左營區1號"


def test_normalize_compound_empty_value(monkeypatch):
    """迄欄是空字串 → W1.5 在 Pass 1 直接 continue，不走拆解"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台ID": "DIRECT_ID",
        "迄基地台": "",  # 空字串
    })
    assert out.get("cell_id") == "DIRECT_ID"
    assert "cell_addr" not in out
