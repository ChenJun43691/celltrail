# backend/app/tests/test_carrier_profile.py
"""
W1 carrier_profile service 單元測試 —— 不依賴 DB 即可執行。

覆蓋目標：
  1. _canon 與 ingest._canon 行為一致（防止兩個副本飄離）
  2. _build_header_map_from_mapping 正確
  3. DB 不可用時 fallback 路徑能拿回完整 _RAW2CANON map
  4. invalidate_cache 後重新讀取
  5. 4 個真實樣本檔的關鍵欄名都能被正確識別（W1 真正要解的問題）

DB 整合測試（需要 PostgreSQL 在線）：放在 test_smoke.py / 整合測試環境。
"""
from __future__ import annotations

import os

# 必須在 import app.* 之前設好環境變數，避免 db.session 在 import 期間崩潰
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# ─────────────────────────────────────────────────────────────
# 1. _canon 對齊測試
# ─────────────────────────────────────────────────────────────
def test_canon_matches_ingest_canon():
    """
    carrier_profile._canon 是 ingest._canon 的副本（為了避免 circular import）。
    這個測試鎖死兩者行為一致；若 ingest._canon 改了規則但忘了同步本檔，這裡會立刻爆。
    """
    from app.services.carrier_profile import _canon as cp_canon
    from app.services.ingest import _canon as ing_canon

    samples = [
        "開始連線時間",
        "基地台/交換機",     # 含斜線（會被 _canon 移除）
        "  始話時間  ",      # 前後空白
        "基地臺位址",         # 「臺」→「台」
        "Cell-ID",            # 連字符
        "ＣＥＬＬ",           # 全形 ASCII
        "細胞．名稱",         # 全形句點
        "azimuth",
        "",
        None,
    ]
    for s in samples:
        assert cp_canon(s) == ing_canon(s), f"_canon 結果不一致：{s!r}"


# ─────────────────────────────────────────────────────────────
# 2. _build_header_map_from_mapping
# ─────────────────────────────────────────────────────────────
def test_build_header_map_keys_are_canon():
    """
    DB 內 mapping_json 是「原始鍵 → canonical」；service 應在記憶體層對 key 做 _canon。
    驗證：build 後的 dict key 都是 canon 過的形式。
    """
    from app.services.carrier_profile import (
        _build_header_map_from_mapping, _canon,
    )

    raw = {
        "開始連線時間": "start_ts",
        "基地台/交換機": "cell_id",
        "ＣＥＬＬ": "sector_id",
    }
    built = _build_header_map_from_mapping(raw)
    assert built[_canon("開始連線時間")] == "start_ts"
    assert built[_canon("基地台/交換機")] == "cell_id"
    assert built[_canon("ＣＥＬＬ")] == "sector_id"
    # 原始鍵應該不存在（因為已 canon）
    assert "基地台/交換機" not in built


def test_build_header_map_handles_empty():
    from app.services.carrier_profile import _build_header_map_from_mapping
    assert _build_header_map_from_mapping({}) == {}
    assert _build_header_map_from_mapping(None) == {}  # type: ignore


# ─────────────────────────────────────────────────────────────
# 3. DB 不可用時的 fallback
# ─────────────────────────────────────────────────────────────
def test_fallback_to_ingest_raw2canon(monkeypatch):
    """
    模擬 DB 連不上 → 應自動 fallback 到 ingest._RAW2CANON。
    驗證 fallback 的 map 能命中 _RAW2CANON 內每一個鍵。
    """
    import app.services.carrier_profile as cp
    from app.services.ingest import _RAW2CANON, _canon

    # 強制 _load_default_profile_from_db 拋例外（模擬 DB 連不上）
    def _raise(*a, **kw):
        raise RuntimeError("DB connection refused (test simulation)")
    monkeypatch.setattr(cp, "_load_default_profile_from_db", _raise)

    # 清 cache 以觸發重新載入
    cp.invalidate_cache()

    header_map = cp.get_active_header_map()
    # _RAW2CANON 每個鍵都應該在 fallback map 內可查
    for raw_key, canonical in _RAW2CANON.items():
        assert header_map.get(_canon(raw_key)) == canonical, (
            f"fallback map 缺漏：{raw_key} → {canonical}"
        )

    cp.invalidate_cache()  # 清掉 test 留下的 cache 影響其他 test


def test_fallback_when_no_default_profile_in_db(monkeypatch):
    """
    模擬 DB 連得上但 carrier_profiles 內沒有 default profile（schema 種子 INSERT 漏掉的情境）
    → 同樣 fallback 到 _RAW2CANON。

    註：不能直接比 len(header_map) == len(_RAW2CANON) — 因為 _RAW2CANON 有 42 個原始
    別名，但經過 _canon 後會 collapse（繁/簡、全/半形重複），實際 header_map 會少於 42。
    正確驗法：「_RAW2CANON 內每一個 entry，canon 後都應在 header_map 找到對應 canonical」。
    """
    import app.services.carrier_profile as cp
    from app.services.carrier_profile import _canon
    from app.services.ingest import _RAW2CANON

    monkeypatch.setattr(cp, "_load_default_profile_from_db", lambda: None)
    cp.invalidate_cache()

    header_map = cp.get_active_header_map()
    for raw_key, canonical in _RAW2CANON.items():
        assert header_map.get(_canon(raw_key)) == canonical, (
            f"fallback map 缺漏：{raw_key} → {canonical}"
        )

    cp.invalidate_cache()


# ─────────────────────────────────────────────────────────────
# 4. invalidate_cache
# ─────────────────────────────────────────────────────────────
def test_invalidate_cache_forces_reload(monkeypatch):
    import app.services.carrier_profile as cp

    call_count = {"n": 0}

    def _fake_load():
        call_count["n"] += 1
        return {
            "id": 1, "carrier_name": None, "variant_label": "default",
            "mapping_json": {"自訂時間欄": "start_ts"},
            "is_default": True, "is_active": True, "notes": None,
            "created_by": None, "approved_by": None, "approved_at": None,
            "llm_assisted": False, "llm_model": None, "llm_prompt_hash": None,
            "created_at": None, "updated_at": None,
        }

    monkeypatch.setattr(cp, "_load_default_profile_from_db", _fake_load)
    cp.invalidate_cache()

    cp.get_active_header_map()
    cp.get_active_header_map()
    cp.get_active_header_map()
    assert call_count["n"] == 1, "cache 未生效，每次都呼叫 DB"

    cp.invalidate_cache()
    cp.get_active_header_map()
    assert call_count["n"] == 2, "invalidate 後應重新查 DB"

    cp.invalidate_cache()


# ─────────────────────────────────────────────────────────────
# 5. 真實樣本檔的關鍵欄名能被識別
# ─────────────────────────────────────────────────────────────
def test_real_sample_headers_are_recognized(monkeypatch):
    """
    這是 W1 的核心驗收：使用者上傳的 4 個樣本檔內，原本被丟掉的欄名
    現在都該被識別。
    """
    import app.services.carrier_profile as cp

    # 強制走 fallback（_RAW2CANON），確保「即使 DB 沒 seed」也通過
    monkeypatch.setattr(cp, "_load_default_profile_from_db", lambda: None)
    cp.invalidate_cache()

    hm = cp.get_active_header_map()
    from app.services.carrier_profile import _canon

    # 樣本 1：「0801-0903彭奕翔網路歷程.xlsx」表頭
    assert hm[_canon("時間")] == "start_ts"
    assert hm[_canon("基地台")] == "cell_id"

    # 樣本 2：「周蔓達上網歷程.xlsx」表頭
    assert hm[_canon("始話時間")] == "start_ts"
    assert hm[_canon("起台")] == "cell_id"
    assert hm[_canon("起址")] == "cell_addr"

    # 樣本 3 + 4：「網路歷程-2a0c1c9a.xltx」「網路歷程.xltx」（既有就能對齊）
    assert hm[_canon("啟始時間")] == "start_ts"
    assert hm[_canon("結束時間")] == "end_ts"
    assert hm[_canon("基地台ID")] == "cell_id"
    assert hm[_canon("最終基地台位址")] == "cell_addr"

    # 樣本 5：「電話通聯+歷程.xlsx」表頭
    assert hm[_canon("基地台/交換機")] == "cell_id"
    # 「始話日期」「迄台」「迄址」目前**故意不收**（W2/W3 會處理）
    assert hm.get(_canon("迄台")) is None, "迄類別名應仍未收（W2 才處理）"
    assert hm.get(_canon("迄址")) is None
    assert hm.get(_canon("始話日期")) is None, "需 ingest 層做日期+時間合併（W2）"

    cp.invalidate_cache()


# ─────────────────────────────────────────────────────────────
# 6. ingest._normalize_row 透過 service 走通
# ─────────────────────────────────────────────────────────────
def test_normalize_row_uses_service(monkeypatch):
    """
    確認 ingest._normalize_row 在 W1 後**真的**會經過 carrier_profile service。
    模擬 service 回一個簡化的 map，看 _normalize_row 是否照著走。
    """
    import app.services.carrier_profile as cp
    from app.services.ingest import _normalize_row, _canon

    # 假裝 DB 給了一個非常陽春的 mapping，只認「我的時間」
    fake_profile = {
        "id": 999, "carrier_name": None, "variant_label": "test_only",
        "mapping_json": {"我的時間": "start_ts"},
        "is_default": True, "is_active": True, "notes": None,
        "created_by": None, "approved_by": None, "approved_at": None,
        "llm_assisted": False, "llm_model": None, "llm_prompt_hash": None,
        "created_at": None, "updated_at": None,
    }
    monkeypatch.setattr(cp, "_load_default_profile_from_db", lambda: fake_profile)
    cp.invalidate_cache()

    raw_row = {"我的時間": "2026-04-27 10:00:00", "其他無關欄": "junk"}
    norm = _normalize_row(raw_row)
    assert norm == {"start_ts": "2026-04-27 10:00:00"}

    cp.invalidate_cache()
