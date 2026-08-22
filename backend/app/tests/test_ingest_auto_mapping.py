# backend/app/tests/test_ingest_auto_mapping.py
"""
`ingest_auto(mapping=)` —— 讓「存檔路徑」也吃得下手動欄位對應（P9 Phase 2B）。

為什麼需要這條路：在此之前，手動對應過的檔案**只能**走 parse-temp + save-records
（前端把解析後的 records 送回 server 落地）—— 那條路沒有原始檔、沒有 SHA-256 證據鏈，
server 也無從驗證前端送回來的東西。preview save 走的是 `ingest_auto`，若它不吃 mapping，
手動對應的檔案在 preview 流程裡會落地成 0 筆（解析階段看得到、存檔階段卻對不上欄位）。

守的三件事：
  1. mapping 確實一路傳到 `_apply_user_mapping`（rename 成 _RAW2CANON alias）。
  2. Excel 分支的 mapping 必須**同時**傳進 `_iter_rows_excel(user_mapping=)` ——
     否則陌生格式的 sheet 會在 rename 之前就被規則 B 丟掉（P8 五-Q 的結構性 bug）。
  3. `mapping=None` 時行為與改動前逐字相同（既有呼叫端零回歸）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")

import pytest

from app.services import ingest


@pytest.fixture
def captured(monkeypatch):
    """攔下 _ingest_rows_stream，把它收到的 rows 具現化後交給測試檢查（不碰 DB）。"""
    box = {}

    def _stream(project_id, target_id, rows_iter):
        box["project_id"] = project_id
        box["target_id"] = target_id
        box["rows"] = list(rows_iter)
        return {"total": len(box["rows"]), "inserted": len(box["rows"]), "skipped": 0, "errors": []}

    monkeypatch.setattr(ingest, "_ingest_rows_stream", _stream)
    return box


CSV_UNKNOWN = "怪欄甲,怪欄乙\n2026-01-01 10:00:00,高雄市前金區中正四路211號\n".encode("utf-8")


def test_csv_mapping_renames_to_canonical_alias(captured):
    """使用者說「怪欄甲是時間、怪欄乙是地址」→ row key 應被 rename 成系統認得的 alias。"""
    ingest.ingest_auto("p", "t", "x.csv", CSV_UNKNOWN,
                       mapping={"怪欄甲": "time", "怪欄乙": "addr"})
    row = captured["rows"][0]
    norm = ingest._normalize_row(row)
    assert norm.get("start_ts") == "2026-01-01 10:00:00"
    assert norm.get("cell_addr") == "高雄市前金區中正四路211號"


def test_csv_without_mapping_leaves_unknown_columns_unmapped(captured):
    """對照組：不給 mapping 時這兩個欄名對系統毫無意義 —— 證明上一條測到的是 mapping 本身。"""
    ingest.ingest_auto("p", "t", "x.csv", CSV_UNKNOWN)
    norm = ingest._normalize_row(captured["rows"][0])
    assert not norm.get("start_ts")
    assert not norm.get("cell_addr")


def test_csv_mapping_ignore_drops_column(captured):
    ingest.ingest_auto("p", "t", "x.csv", CSV_UNKNOWN,
                       mapping={"怪欄甲": "time", "怪欄乙": "ignore"})
    row = captured["rows"][0]
    assert "怪欄乙" not in row
    assert ingest._normalize_row(row).get("cell_addr") in (None, "")


def test_excel_branch_passes_user_mapping_into_iter_rows(monkeypatch, captured):
    """Excel 分支必須把 mapping 傳給 _iter_rows_excel —— 陌生 sheet 才不會在 rename 前被丟棄。"""
    seen = {}

    def _fake_iter(file_bytes, user_mapping=None):
        seen["user_mapping"] = user_mapping
        yield {"怪欄甲": "2026-01-01 10:00:00"}

    monkeypatch.setattr(ingest, "_iter_rows_excel", _fake_iter)
    monkeypatch.setattr(ingest, "_reject_if_encrypted", lambda b: None)
    ingest.ingest_auto("p", "t", "x.xlsx", b"PK\x03\x04", mapping={"怪欄甲": "time"})
    assert seen["user_mapping"] == {"怪欄甲": "time"}
    assert ingest._normalize_row(captured["rows"][0]).get("start_ts") == "2026-01-01 10:00:00"


def test_excel_without_mapping_passes_none(monkeypatch, captured):
    seen = {}

    def _fake_iter(file_bytes, user_mapping=None):
        seen["user_mapping"] = user_mapping
        yield {"時間": "2026-01-01 10:00:00"}

    monkeypatch.setattr(ingest, "_iter_rows_excel", _fake_iter)
    monkeypatch.setattr(ingest, "_reject_if_encrypted", lambda b: None)
    ingest.ingest_auto("p", "t", "x.xlsx", b"PK\x03\x04")
    assert seen["user_mapping"] is None


def test_pdf_with_mapping_rejected(monkeypatch):
    """與 parse_file_only 同一條界線：PDF 沒有 raw column name 可 rename。
    要點是**明確拒絕**而不是默默忽略 mapping —— 後者會讓使用者以為對應生效了。"""
    monkeypatch.setattr(ingest, "_reject_if_encrypted", lambda b: None)
    monkeypatch.setattr(ingest, "ingest_pdf",
                        lambda *a: pytest.fail("帶 mapping 的 PDF 不應進入 ingest_pdf"))
    with pytest.raises(ValueError, match="PDF"):
        ingest.ingest_auto("p", "t", "x.pdf", b"%PDF-1.4", mapping={"a": "time"})


def test_pdf_without_mapping_still_works(monkeypatch):
    monkeypatch.setattr(ingest, "_reject_if_encrypted", lambda b: None)
    monkeypatch.setattr(ingest, "ingest_pdf", lambda *a: {"total": 3, "inserted": 3, "skipped": 0})
    assert ingest.ingest_auto("p", "t", "x.pdf", b"%PDF-1.4")["inserted"] == 3


def test_unsupported_ext_unchanged(monkeypatch):
    monkeypatch.setattr(ingest, "_reject_if_encrypted", lambda b: None)
    with pytest.raises(ValueError, match="不支援的檔案格式"):
        ingest.ingest_auto("p", "t", "x.docx", b"zz", mapping={"a": "time"})


def test_signature_is_backward_compatible(captured):
    """既有呼叫端（upload.py 等）全部是位置參數 4 個 —— mapping 必須是有預設的第 5 個。"""
    import inspect

    sig = inspect.signature(ingest.ingest_auto)
    params = list(sig.parameters)
    assert params[:4] == ["project_id", "target_id", "filename", "file_bytes"]
    assert params[4] == "mapping"
    assert sig.parameters["mapping"].default is None
