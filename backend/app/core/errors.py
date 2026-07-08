# backend/app/core/errors.py
"""
統一 API error contract（P9 Phase 2A.3）。

集中定義 machine-readable error code、AppError 例外、以及 error response body 組裝，
讓 controller 只 raise AppError、不散落中文 detail 與裸 dict。

Error response 形狀（見 schemas.preview.ErrorResponse）：
    {
      "error": {"code": "PREVIEW_EXPIRED", "message": "...", "details": {}},
      "request_id": "req_xxx"
    }

安全原則：
  - message 給人看（可中文）；code 給程式判斷。
  - details 只放非敏感資訊；絕不放 SQL / stack trace / JWT / API key / raw PII。
  - status_code 沿用既有語意（401/403/404/409/410/413/422/503/500）。

本輪只套用到 preview 路徑；其餘 endpoint 的既有 HTTPException 行為不受影響。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# ── Error codes（machine-readable；前端據此判斷，不再靠中文字串）──
class ErrorCode:
    # preview 生命週期
    PREVIEW_NOT_FOUND = "PREVIEW_NOT_FOUND"
    PREVIEW_FORBIDDEN = "PREVIEW_FORBIDDEN"
    PREVIEW_EXPIRED = "PREVIEW_EXPIRED"
    PREVIEW_REVOKED = "PREVIEW_REVOKED"
    PREVIEW_CONSUMED = "PREVIEW_CONSUMED"
    PREVIEW_TOO_LARGE = "PREVIEW_TOO_LARGE"
    PREVIEW_STORAGE_UNAVAILABLE = "PREVIEW_STORAGE_UNAVAILABLE"
    PREVIEW_KEY_MISSING = "PREVIEW_KEY_MISSING"
    PREVIEW_SHA_MISMATCH = "PREVIEW_SHA_MISMATCH"
    PREVIEW_PARSE_FAILED = "PREVIEW_PARSE_FAILED"
    # 通用
    AUTH_REQUIRED = "AUTH_REQUIRED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# 410 三態集合（前端據 code 直接判斷，不需中文字串）。
GONE_CODES = frozenset({
    ErrorCode.PREVIEW_EXPIRED,
    ErrorCode.PREVIEW_REVOKED,
    ErrorCode.PREVIEW_CONSUMED,
})


class AppError(Exception):
    """應用層可預期錯誤：帶 machine-readable code + 使用者訊息 + HTTP status。

    controller raise 之後由 core.error_handlers 的 handler 轉成統一 response。
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def error_body(
    code: str,
    message: str,
    request_id: Optional[str],
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """組裝統一 error response body（唯一組裝點，避免各處手拼）。"""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
        "request_id": request_id,
    }


# ── status_code → 通用 code（供 preview-scoped HTTPException 轉譯用）──
# preview.py 內部改 raise AppError；此表只服務「非 AppError 的 HTTPException」
# （例如 get_current_user 的 401、assert_project_access 的 403）落在 preview 路徑時的轉譯。
_HTTP_STATUS_TO_CODE = {
    401: ErrorCode.AUTH_REQUIRED,
    403: ErrorCode.PREVIEW_FORBIDDEN,
    404: ErrorCode.PREVIEW_NOT_FOUND,
    409: ErrorCode.PREVIEW_SHA_MISMATCH,
    413: ErrorCode.PREVIEW_TOO_LARGE,
    503: ErrorCode.PREVIEW_STORAGE_UNAVAILABLE,
}


def code_for_status(status_code: int) -> str:
    """把 preview 路徑上的非 AppError HTTPException status 映射成 machine-readable code。"""
    return _HTTP_STATUS_TO_CODE.get(status_code, ErrorCode.INTERNAL_ERROR)
