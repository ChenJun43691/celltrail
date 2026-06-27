# backend/app/tests/test_ingest_chunked_stream.py
"""
chunk-based 串流匯入（P8.1，2026-06-28）：_ingest_rows_stream 改造回歸測試。

驗證 /upload 存檔路徑改為分塊後：
  - 每塊用 geocode.lookup_bulk（並行 + SQL 快取），不再逐筆 geocode.lookup
  - 記憶體不累積整檔（每塊 flush 後釋放）
  - 統計 total/inserted/skipped 與舊版語意一致
  - 原子性方案 A：某塊寫入失敗 → 停止後續 + 誠實回報「部分匯入」

不碰真 DB / 網路：monkeypatch `_insert_records`（spy）與 `geocode.lookup_bulk`（spy），
並把 carrier_profile fallback 到 _RAW2CANON（避免 _normalize_row 碰 DB）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


def _patch_active_map_to_default(monkeypatch):
    import app.services.carrier_profile as cp

    monkeypatch.setattr(cp, "_load_default_profile_from_db", lambda: None)
    cp.invalidate_cache()


@pytest.fixture
def spies(monkeypatch):
    """patch _insert_records 與 geocode.lookup_bulk 為 spy，回傳記錄容器。"""
    import app.services.ingest as ing

    insert_calls: List[List[Dict[str, Any]]] = []   # 每次 _insert_records 收到的 records
    bulk_calls: List[list] = []                      # 每次 lookup_bulk 收到的 keys

    def fake_insert(records):
        # 深拷貝 key 集合即可；記錄這批 records，回傳「寫入筆數」
        insert_calls.append(list(records))
        return len(records)

    def fake_bulk(keys):
        bulk_calls.append(list(keys))
        return {k: (25.0, 121.5) for k in keys}

    monkeypatch.setattr(ing, "_insert_records", fake_insert)
    monkeypatch.setattr(ing.geocode, "lookup_bulk", fake_bulk)
    return {"insert": insert_calls, "bulk": bulk_calls}


def _row(ts="2026-01-01 10:00:00", cell="CELL_A", addr="台北市A路1號"):
    """組一列 raw（_RAW2CANON 認得的欄名）。"""
    d: Dict[str, Any] = {"開始時間": ts}
    if cell is not None:
        d["基地台編號"] = cell
    if addr is not None:
        d["基地台地址"] = addr
    return d


def _run(monkeypatch, rows, chunk=None):
    """跑 _ingest_rows_stream；chunk 直接 patch _ingest_chunk_size（繞過 env 合法範圍
    100~5000 的 clamp，方便用小數字驗證分塊邏輯）。env 解析/clamp 另有專測。"""
    _patch_active_map_to_default(monkeypatch)
    import app.services.ingest as ing
    if chunk is not None:
        monkeypatch.setattr(ing, "_ingest_chunk_size", lambda: chunk)
    return ing._ingest_rows_stream("proj", "tgt", iter(rows))


# ── a. 小於 chunk size 的小檔 ─────────────────────────────
def test_a_smaller_than_chunk(monkeypatch, spies):
    rows = [_row(cell=f"C{i}", addr=f"台北市{i}號") for i in range(2)]
    res = _run(monkeypatch, rows, chunk=3)
    assert res == {"total": 2, "inserted": 2, "skipped": 0, "errors": []}
    assert len(spies["insert"]) == 1            # 只一塊
    assert len(spies["insert"][0]) == 2
    assert len(spies["bulk"]) == 1              # 一次 bulk geocode


# ── b. 大於 chunk size 的多 chunk 檔 ─────────────────────
def test_b_multi_chunk(monkeypatch, spies):
    rows = [_row(cell=f"C{i}", addr=f"台北市{i}號") for i in range(7)]
    res = _run(monkeypatch, rows, chunk=3)
    assert res["total"] == 7 and res["inserted"] == 7 and res["skipped"] == 0
    # 3 + 3 + 1 → 3 次 insert、3 次 bulk
    assert [len(c) for c in spies["insert"]] == [3, 3, 1]
    assert len(spies["bulk"]) == 3


# ── c. chunk size 剛好整除 ───────────────────────────────
def test_c_exact_multiple(monkeypatch, spies):
    rows = [_row(cell=f"C{i}", addr=f"台北市{i}號") for i in range(6)]
    res = _run(monkeypatch, rows, chunk=3)
    assert res["inserted"] == 6
    assert [len(c) for c in spies["insert"]] == [3, 3]   # 收尾空 flush 不應多一次


# ── d. chunk size + 1 ────────────────────────────────────
def test_d_chunk_plus_one(monkeypatch, spies):
    rows = [_row(cell=f"C{i}", addr=f"台北市{i}號") for i in range(4)]
    res = _run(monkeypatch, rows, chunk=3)
    assert res["inserted"] == 4
    assert [len(c) for c in spies["insert"]] == [3, 1]


# ── e. 全部 skip（缺開始時間）─────────────────────────────
def test_e_all_skipped(monkeypatch, spies):
    rows = [{"基地台編號": f"C{i}", "基地台地址": "台北市X路"} for i in range(5)]  # 無時間
    res = _run(monkeypatch, rows, chunk=3)
    assert res["total"] == 5 and res["inserted"] == 0 and res["skipped"] == 5
    assert spies["insert"] == []   # 完全沒寫
    assert spies["bulk"] == []     # 完全沒 geocode


# ── f. lat/lng 直給 → 不應打 geocode ─────────────────────
def test_f_direct_latlng_no_geocode(monkeypatch, spies):
    rows = [
        {"開始時間": "2026-01-01 10:00:00", "緯度": "25.04", "經度": "121.5"}
        for _ in range(3)
    ]
    res = _run(monkeypatch, rows, chunk=10)
    assert res["inserted"] == 3
    assert spies["bulk"] == []     # 直給座標，lookup_bulk 完全沒被呼叫
    # 寫入的 record 應帶座標
    rec = spies["insert"][0][0]
    assert rec["lat"] is not None and rec["lng"] is not None


# ── g. 同址在同 chunk 內只進 lookup_bulk 一次 ────────────
def test_g_same_addr_dedup_in_chunk(monkeypatch, spies):
    rows = [_row(cell="SAME", addr="台北市同一路1號") for _ in range(3)]
    res = _run(monkeypatch, rows, chunk=10)
    assert res["inserted"] == 3
    assert len(spies["bulk"]) == 1
    assert spies["bulk"][0] == [("SAME", "台北市同一路1號")]   # 去重後只 1 個 key


# ── 額外：bulk 結果正確回填座標 ───────────────────────────
def test_geocode_result_filled(monkeypatch, spies):
    rows = [_row(cell="C1", addr="台北市A路1號")]
    _run(monkeypatch, rows, chunk=10)
    rec = spies["insert"][0][0]
    assert (rec["lat"], rec["lng"]) == (25.0, 121.5)
    assert "_geo_key" not in rec   # reserved key 應已 pop 掉，不會污染 DB 寫入


# ── 方案 A：某塊寫入失敗 → 部分匯入、停止後續、誠實回報 ───
def test_partial_import_on_chunk_failure(monkeypatch):
    _patch_active_map_to_default(monkeypatch)
    import app.services.ingest as ing
    from fastapi import HTTPException

    monkeypatch.setattr(ing, "_ingest_chunk_size", lambda: 3)
    monkeypatch.setattr(ing.geocode, "lookup_bulk", lambda keys: {k: (25.0, 121.5) for k in keys})

    calls = {"n": 0}

    def flaky_insert(records):
        calls["n"] += 1
        if calls["n"] == 2:   # 第 2 塊失敗
            raise HTTPException(status_code=400, detail="模擬 DB 寫入失敗")
        return len(records)

    monkeypatch.setattr(ing, "_insert_records", flaky_insert)

    rows = [_row(cell=f"C{i}", addr=f"台北市{i}號") for i in range(7)]  # 3+3+1
    res = ing._ingest_rows_stream("proj", "tgt", iter(rows))

    # 第 1 塊成功(3)、第 2 塊失敗 → 停止、第 3 塊不處理
    assert res["inserted"] == 3, f"應只計成功的第 1 塊，實際 {res['inserted']}"
    assert res["inserted"] < res["total"]            # 誠實：inserted < total
    assert res["errors"], "應有錯誤訊息"
    assert "部分匯入" in res["errors"][0]            # 首列標明、survive [:50]
    assert calls["n"] == 2, "第 2 塊失敗後不應再呼叫第 3 塊"


# ── _ingest_chunk_size：env 解析與 clamp（預設 800、合理 100~5000、非法 fallback）─
@pytest.mark.parametrize("env_val,expected", [
    (None, 800),       # 未設 → 預設
    ("800", 800),
    ("100", 100),      # 下界
    ("5000", 5000),    # 上界
    ("1500", 1500),
    ("99", 800),       # 太小 → fallback
    ("5001", 800),     # 太大 → fallback
    ("0", 800),
    ("-5", 800),
    ("abc", 800),      # 非數字 → fallback
    ("", 800),
])
def test_chunk_size_env_clamp(monkeypatch, env_val, expected):
    from app.services.ingest import _ingest_chunk_size

    if env_val is None:
        monkeypatch.delenv("INGEST_CHUNK_SIZE", raising=False)
    else:
        monkeypatch.setenv("INGEST_CHUNK_SIZE", env_val)
    assert _ingest_chunk_size() == expected
