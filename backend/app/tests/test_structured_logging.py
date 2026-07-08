# backend/app/tests/test_structured_logging.py
"""
core.logging_utils 單元測試（P9 Phase 2A.3）：JSON 格式、共同欄位、遮罩、redaction。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core import logging_utils as log


def _records(caplog):
    out = []
    for rec in caplog.records:
        if rec.name != "celltrail":
            continue
        try:
            out.append(json.loads(rec.getMessage()))
        except Exception:
            pass
    return out


# 23 & 24. log 為合法 JSON，含 event/level/request_id/timestamp
def test_log_is_valid_json_with_common_fields(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        log.log_info("preview.create.ok", plotted=3)
    evts = _records(caplog)
    assert len(evts) == 1
    e = evts[0]
    for k in ("timestamp", "level", "event", "request_id"):
        assert k in e
    assert e["level"] == "INFO"
    assert e["event"] == "preview.create.ok"
    assert e["plotted"] == 3


def test_levels(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        log.log_info("e.info")
        log.log_warning("e.warn")
        log.log_error("e.err")
    levels = {e["event"]: e["level"] for e in _records(caplog)}
    assert levels == {"e.info": "INFO", "e.warn": "WARNING", "e.err": "ERROR"}


# 25. 不記 Authorization / token / key / raw bytes
def test_redacts_sensitive_fields(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        log.log_info(
            "preview.create.ok",
            authorization="Bearer SECRET-JWT",
            access_token="tok-123",
            preview_artifact_key="deadbeef",
            api_key="k",
            jwt="j",
            raw=b"\x00\x01rawbytes",
            plotted=2,
        )
    e = _records(caplog)[0]
    dumped = json.dumps(e)
    assert "SECRET-JWT" not in dumped
    assert "tok-123" not in dumped
    assert "deadbeef" not in dumped
    assert "rawbytes" not in dumped
    for k in ("authorization", "access_token", "preview_artifact_key", "api_key", "jwt", "raw"):
        assert k not in e
    assert e["plotted"] == 2   # 非敏感欄位保留


# 26. preview_id 遮罩
def test_mask_preview_id():
    assert log.mask_preview_id(None) is None
    assert log.mask_preview_id("short") == "***"
    masked = log.mask_preview_id("tok_abcdef1234567890xyz")
    assert masked.startswith("tok_ab") and masked.endswith("0xyz")
    assert "abcdef1234567890" not in masked


# 27（補充）. bytearray 也被丟棄
def test_bytearray_dropped(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        log.log_info("e", blob=bytearray(b"abc"), ok=1)
    e = _records(caplog)[0]
    assert "blob" not in e and e["ok"] == 1


# 25b. 精確 redaction：api_key / preview_artifact_key 移除，但 *_key 一般欄位保留
def test_redaction_precise_not_over_broad(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        log.log_info(
            "e",
            api_key="AK-123",
            preview_artifact_key="PAK-xyz",
            cache_key="ck",
            lookup_key="lk",
            parser_key="pk",
            storage_kind="db",
        )
    e = _records(caplog)[0]
    # 敏感 → 移除
    assert "api_key" not in e
    assert "preview_artifact_key" not in e
    dumped = json.dumps(e)
    assert "AK-123" not in dumped and "PAK-xyz" not in dumped
    # 一般 *_key 欄位 → 保留（不過度清除）
    assert e["cache_key"] == "ck"
    assert e["lookup_key"] == "lk"
    assert e["parser_key"] == "pk"
    assert e["storage_kind"] == "db"


# 25c. 欄位名稱完全等於 "key" / "auth" / "credential" → 移除
def test_redaction_exact_names(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        log.log_info("e", key="k", auth="a", credential="c", ok=1)
    e = _records(caplog)[0]
    for bad in ("key", "auth", "credential"):
        assert bad not in e
    assert e["ok"] == 1
