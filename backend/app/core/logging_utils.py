# backend/app/core/logging_utils.py
"""
結構化 logging（P9 Phase 2A.3）。

輸出單行 JSON 到 stdout（Render 由平台收集；未來可直接餵 ELK / Loki）。
共同欄位：timestamp / level / event / request_id。其餘欄位由呼叫端以 **fields 帶入。

安全：
  - 內建 redaction：key 命中敏感詞（authorization/token/secret/password/jwt/cookie/api_key）→ 丟棄。
  - bytes 值一律丟棄（避免 raw file bytes 進 log）。
  - preview_id 請用 mask_preview_id() 遮罩後再帶入（前 6 + 後 4）。
  - filename 可能含個資：預設不記完整值，需要時只記副檔名。

本輪只在 preview 路徑與 preview cleanup scheduler 使用；不一次改全專案 print()。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict

_LOGGER_NAME = "celltrail"
_logger = logging.getLogger(_LOGGER_NAME)

# key 命中這些子字串（不分大小寫）即視為敏感 → 不寫入 log。
# 精確規則：不用裸 "key"（會誤刪 cache_key / lookup_key / parser_key 等正常欄位），
# 改以明確的敏感子字串涵蓋 api_key / apikey / preview_artifact_key（artifact_key）等。
_SENSITIVE_KEY_PARTS = (
    "authorization", "token", "secret", "password", "jwt", "cookie", "bearer",
    "api_key", "apikey", "artifact_key",
)

# 欄位名稱「完全等於」這些（正規化後）即視為敏感 → 不寫入 log。
_SENSITIVE_EXACT = frozenset({"key", "auth", "credential", "credentials"})


def _ensure_handler() -> None:
    """對 celltrail logger 掛一個 stdout JSON handler（冪等）。"""
    if getattr(_logger, "_celltrail_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = True  # 讓 caplog / root 觀測（production root 無 handler → 不重複）
    _logger._celltrail_configured = True  # type: ignore[attr-defined]


_ensure_handler()


def mask_preview_id(preview_id: Any) -> Any:
    """遮罩 preview_id：短則全遮，長則前 6 + … + 後 4。None 原樣回傳。"""
    if preview_id is None:
        return None
    s = str(preview_id)
    if len(s) <= 10:
        return "***"
    return s[:6] + "…" + s[-4:]


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if k in _SENSITIVE_EXACT:
        return True
    return any(part in k for part in _SENSITIVE_KEY_PARTS)


def _redact(fields: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for k, v in fields.items():
        if _is_sensitive_key(k):
            continue
        if isinstance(v, (bytes, bytearray)):
            continue          # 絕不記 raw bytes
        clean[k] = v
    return clean


def _emit(level: int, level_name: str, event: str, **fields: Any) -> None:
    # 延後 import 避免與 request_context 循環依賴。
    try:
        from app.core.request_context import get_request_id
        rid = get_request_id()
    except Exception:  # pragma: no cover - defensive
        rid = None

    record: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level_name,
        "event": event,
        "request_id": rid,
    }
    record.update(_redact(fields))
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - defensive
        line = json.dumps({"level": level_name, "event": event, "log_error": "serialize_failed"})
    _logger.log(level, line)


def log_info(event: str, **fields: Any) -> None:
    _emit(logging.INFO, "INFO", event, **fields)


def log_warning(event: str, **fields: Any) -> None:
    _emit(logging.WARNING, "WARNING", event, **fields)


def log_error(event: str, **fields: Any) -> None:
    _emit(logging.ERROR, "ERROR", event, **fields)
