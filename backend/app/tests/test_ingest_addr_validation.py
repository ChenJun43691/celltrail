"""
_normalize_row 內容驗證單元測試（2026-05-24）

守護 commit「ingest：cell_addr hex 短碼改寫至 sector_id」。

背景（見 WAKE_UP_TODO 第 7 條 + 0517test 案件）：
  台哥大-第二類.xlsx 同時有 2 欄都被 _RAW2CANON 映到 cell_addr —
  「起址」吐 hex（如 `0E2921B7`），「基地台位址」吐真地址。W1.5「空值
  不覆蓋」紀律下 99%+ 的列會被真地址覆蓋；但若真地址欄該列為空，
  hex 會殘留下來 → coverage 把它歸到 addr_geocode_failed（讓使用者誤以為
  「地址查不到」），而真因是「根本不是地址」。

策略（_normalize_row Pass 1）：cell_addr 收到純 hex 短碼（6–12 chars）
時不寫入 cell_addr，改寫到 sector_id（若仍空）。讓 coverage 正確歸到
cellid_only 類（需業者表）而非 addr_geocode_failed。

不依賴 DB；用 monkeypatch 強制走 _RAW2CANON fallback。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


def _patch_active_map_to_default(monkeypatch):
    import app.services.carrier_profile as cp
    monkeypatch.setattr(cp, "_load_default_profile_from_db", lambda: None)
    cp.invalidate_cache()


# ─────────────────────────────────────────────────────────────
# A. hex 短碼分流到 sector_id
# ─────────────────────────────────────────────────────────────
def test_hex_short_code_in_cell_addr_routed_to_sector_id(monkeypatch):
    """純 hex 8 字元（典型 LAC+CI 表現）→ 不進 cell_addr、寫到 sector_id"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台ID": "CID_001",
        "起址": "0E2921B7",   # 0517test 實測 hex 樣本
    })
    assert out.get("cell_id") == "CID_001"
    assert "cell_addr" not in out, "hex 短碼絕不能殘留在 cell_addr"
    assert out.get("sector_id") == "0E2921B7", (
        "hex 短碼應改寫到 sector_id 保留資訊"
    )


def test_real_address_wins_over_hex_when_both_present(monkeypatch):
    """
    雙欄都映 cell_addr：hex（起址）+ 真地址（基地台位址）
    → 真地址寫入 cell_addr、hex 進 sector_id（W1.5 空值不覆蓋 + 本 fix
       同時作用，順序由 dict 迭代決定但兩種順序都應正確）
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    # 先 hex 再真地址
    out_a = _normalize_row({
        "起址": "0E2921B7",
        "基地台位址": "高雄市苓雅區四維三路2號",
    })
    assert out_a.get("cell_addr") == "高雄市苓雅區四維三路2號"
    assert out_a.get("sector_id") == "0E2921B7"

    # 先真地址再 hex（驗證 fix 不會誤把 hex 寫到 sector_id 之後再被覆蓋掉）
    out_b = _normalize_row({
        "基地台位址": "高雄市苓雅區四維三路2號",
        "起址": "0E2921B7",
    })
    assert out_b.get("cell_addr") == "高雄市苓雅區四維三路2號"
    assert out_b.get("sector_id") == "0E2921B7"


def test_hex_does_not_overwrite_existing_sector_id(monkeypatch):
    """sector_id 已由「細胞」欄填入 → hex 不覆蓋（保留原 sector_id）"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "細胞": "REAL_SECTOR",   # 直接映 sector_id
        "起址": "0E2921B7",       # 試圖塞 cell_addr → 被攔截 → 進 sector_id？
    })
    assert out.get("sector_id") == "REAL_SECTOR", (
        "已有 sector_id 時，hex fallback 不應覆蓋"
    )
    assert "cell_addr" not in out


# ─────────────────────────────────────────────────────────────
# B. 真地址不被誤殺
# ─────────────────────────────────────────────────────────────
def test_real_address_not_affected(monkeypatch):
    """真實中文地址（含區/路/號）不命中 hex pattern → 正常寫入 cell_addr"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台位址": "高雄市苓雅區四維三路2號",
    })
    assert out.get("cell_addr") == "高雄市苓雅區四維三路2號"
    assert "sector_id" not in out


def test_long_alphanumeric_address_id_not_treated_as_hex(monkeypatch):
    """
    13+ 字元的 alphanumeric（如純數字 cell_id 不慎被當地址送來）
    → 超出 6–12 範圍、不命中 hex pattern → 仍寫入 cell_addr
    （這類 case 由下游 geocode_failed 處理，不在本 fix 守備範圍）
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台位址": "46601493130200051012",   # 20 字元純數字
    })
    assert out.get("cell_addr") == "46601493130200051012"


def test_hex_with_surrounding_whitespace_still_detected(monkeypatch):
    """前後空白的 hex 仍被識別（strip 後比對）"""
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "起址": "  0E2921B7  ",
    })
    assert "cell_addr" not in out
    assert out.get("sector_id") == "0E2921B7"


def test_short_hex_5_chars_below_threshold(monkeypatch):
    """5 字元 hex 低於 6 字元下限 → 不視為 LAC+CI → 仍寫 cell_addr

    （避免把意外的短代碼 / 路名片段誤判，門檻刻意保守）
    """
    _patch_active_map_to_default(monkeypatch)
    from app.services.ingest import _normalize_row

    out = _normalize_row({
        "基地台位址": "ABCDE",
    })
    assert out.get("cell_addr") == "ABCDE"
