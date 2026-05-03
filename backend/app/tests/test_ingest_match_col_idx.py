# backend/app/tests/test_ingest_match_col_idx.py
"""
W2.5 _match_col_idx 兩階段比對測試（2026-04-29）

修補 PDF ingest 的子字串歧義 bug：
  - 楊云豪黑莓卡 PDF header 含「細胞名稱」+「細胞」兩欄並存
  - 舊邏輯 `any(c in name for c in cs)` 會讓 `cands["cid"]=["細胞",...]` 在
    「細胞名稱」上提早命中，導致 sector / cid 對到同一 index → silent
    data corruption（sector_id 被錯填為「東港東方」這類 sector_name 值）。

新邏輯（兩階段）：
  Pass 1 精確匹配（canon equal）：每個 cands key 找完全相同的 header。
  Pass 2 子字串備援：未命中的 key 才走子字串，且跳過 Pass 1 已認領的 index。

設計理由：
  - 精確匹配優先 → 「細胞名稱」與「細胞」並存時各認各的 index，無衝突。
  - 子字串備援保留 → 「基地臺編號」（臺）vs「基地台編號」（台）的異體字
                     容錯不丟失。
  - 已認領 index 排除 → 即使子字串匹配，也不會搶走精確匹配的結果。
"""
from __future__ import annotations

import os

# 必須在 import app.* 之前設好環境變數
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# ─────────────────────────────────────────────────────────────
# A. 楊云豪 PDF 真實 header pattern：bug 直接觸發場景
# ─────────────────────────────────────────────────────────────
def test_match_col_idx_no_substring_collision_yang_pdf():
    """
    楊云豪黑莓卡 PDF：『細胞名稱』+『細胞』並存。
    bug 場景下 sector 與 cid 會撞到同一 index 4，
    修法後必須各歸各位（sector→4, cid→6）。
    """
    from app.services.ingest import _match_col_idx

    hdr = [
        "開始連線時間", "結束連線時間",
        "基地台編號", "基地台地址",
        "細胞名稱",          # idx 4 → sector
        "台號",              # idx 5 → site
        "細胞",              # idx 6 → cid (bug 場景下會跑到 4)
        "方位",              # idx 7 → az
    ]
    col = _match_col_idx(hdr)

    assert col["start"]  == 0, f"start expected 0, got {col['start']}"
    assert col["end"]    == 1, f"end expected 1, got {col['end']}"
    assert col["cellid"] == 2, f"cellid expected 2, got {col['cellid']}"
    assert col["addr"]   == 3, f"addr expected 3, got {col['addr']}"
    assert col["sector"] == 4, f"sector expected 4, got {col['sector']}"
    assert col["site"]   == 5, f"site expected 5, got {col['site']}"
    assert col["cid"]    == 6, f"cid expected 6 (the actual 細胞 column), got {col['cid']}"
    assert col["az"]     == 7, f"az expected 7, got {col['az']}"

    # 不可有兩個 cands key 共用同一個非 -1 index
    used = [v for v in col.values() if v >= 0]
    assert len(used) == len(set(used)), f"duplicate index assigned: {col}"


# ─────────────────────────────────────────────────────────────
# B. 向後相容：異體字 / 子字串備援仍可命中
# ─────────────────────────────────────────────────────────────
def test_match_col_idx_traditional_variant_still_works():
    """
    『基地臺編號』（臺）vs『基地台編號』（台）異體字場景：
    Pass 1 精確匹配下會直接命中（cands 已含『基地臺編號』）。
    """
    from app.services.ingest import _match_col_idx

    hdr = ["時間", "基地臺編號", "基地臺地址"]
    col = _match_col_idx(hdr)
    assert col["cellid"] == 1
    assert col["addr"] == 2


def test_match_col_idx_loose_match_via_substring():
    """
    Pass 2 子字串備援：『連線開始時間』不在 cands 精確列表中，
    但『開始時間』是其子字串 → 應由 Pass 2 命中。
    """
    from app.services.ingest import _match_col_idx

    # 注意：這是「開始時間」是「連線開始時間」的子字串的場景
    hdr = ["連線開始時間", "結束時間", "基地台編號"]
    col = _match_col_idx(hdr)
    # cands["start"] 含 "開始時間"，"開始時間" in "連線開始時間" → 命中
    assert col["start"] == 0
    assert col["end"] == 1
    assert col["cellid"] == 2


# ─────────────────────────────────────────────────────────────
# C. 邊界：缺欄、空 header、單欄重複命中
# ─────────────────────────────────────────────────────────────
def test_match_col_idx_missing_columns_return_minus_one():
    """缺欄要回 -1，不可用其他欄位充數。"""
    from app.services.ingest import _match_col_idx

    hdr = ["開始時間", "基地台編號"]   # 沒有 addr/sector/site/cid/az/end
    col = _match_col_idx(hdr)
    assert col["start"] == 0
    assert col["cellid"] == 1
    # end 沒有 → -1（不可被 start 重複指派）
    assert col["end"] == -1
    assert col["addr"] == -1
    assert col["sector"] == -1
    assert col["site"] == -1
    assert col["cid"] == -1
    assert col["az"] == -1


def test_match_col_idx_only_loose_substring_no_collision():
    """
    只有『細胞名稱』沒有『細胞』時，cid 應 fallback 到子字串命中『細胞名稱』。
    這保留了「現場欄位用語不一致時也能盡力對應」的容錯，但不會與 sector 衝突
    （因為 sector 已在 Pass 1 認領了該 index，cid 會留 -1）。
    """
    from app.services.ingest import _match_col_idx

    hdr = ["開始時間", "基地台編號", "細胞名稱"]
    col = _match_col_idx(hdr)
    assert col["sector"] == 2
    # cid 子字串 "細胞" in "細胞名稱"，但 idx 2 已被 sector 認領 → cid -1
    assert col["cid"] == -1
