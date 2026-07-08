# backend/app/core/request_context.py
"""
Request ID 傳遞 + 未預期例外邊界（P9 Phase 2A.3）。

- 每個 HTTP request 產生（或沿用合法的 X-Request-ID）一個 request_id，放進
  contextvars.ContextVar，讓 API / service / logging / error handler 都能取到同一個 id。
- 每個 response 回寫 `X-Request-ID`。
- 未被 registered handler 處理的例外（真正的 500）在此攔截：log 結構化 ERROR、
  回統一 INTERNAL_ERROR contract（含 request_id）、絕不回 stack trace。

為什麼未預期例外邊界放這裡而非 @app.exception_handler(Exception)：
  BaseHTTPMiddleware 在 call_next 前 set contextvar、finally reset。若把 generic
  handler 留在最外層 ServerErrorMiddleware，例外會先傳出本 middleware（contextvar 已
  被 reset）才進 handler → 拿不到 request_id、也無法補 header。故在此就地攔截，確保
  500 一定帶 request_id 與 header。AppError / HTTPException 由 registered handler 在
  call_next 內轉成 response，header 仍由本 middleware 補齊。

stateless / multi-worker safe / async-safe：只用 ContextVar，無全域 mutable dict、不依賴 Redis。
"""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.errors import AppError, ErrorCode, error_body
from app.core import logging_utils as log

REQUEST_ID_HEADER = "X-Request-ID"

# 合法 client request id：只允許可安全寫進 log 的字元，限長度（防 log injection）。
_VALID_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

_request_id_var: ContextVar[Optional[str]] = ContextVar("celltrail_request_id", default=None)


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex


def get_request_id() -> Optional[str]:
    """任何層（service / logging / handler）取得當前 request 的 id。"""
    return _request_id_var.get()


def set_request_id(value: str) -> object:
    """設定 contextvar，回傳 token 供 reset（供 scheduler job 等非 HTTP 情境使用）。"""
    return _request_id_var.set(value)


def reset_request_id(token: object) -> None:
    _request_id_var.reset(token)


def _resolve_incoming(request: Request) -> str:
    incoming = request.headers.get(REQUEST_ID_HEADER)
    if incoming and _VALID_ID.match(incoming):
        return incoming
    return new_request_id()


class RequestContextMiddleware(BaseHTTPMiddleware):
    # 此 middleware 是所有未預期 exception 的最外層安全邊界，
    # 目的在確保 response body 與 X-Request-ID 使用同一 request context。
    async def dispatch(self, request: Request, call_next):
        rid = _resolve_incoming(request)
        token = _request_id_var.set(rid)
        try:
            try:
                response: Response = await call_next(request)
            except AppError as exc:
                # 極少數 AppError 未被 registered handler 攔到時的保險（正常應由 handler 處理）。
                response = JSONResponse(
                    status_code=exc.status_code,
                    content=error_body(exc.code, exc.message, rid, exc.details),
                )
            except Exception as exc:  # noqa: BLE001 — 未預期例外邊界
                # 結構化 log（server 端）；response 只回統一 INTERNAL_ERROR、不含 stack trace。
                log.log_error(
                    "request.unhandled_exception",
                    method=request.method,
                    path=request.url.path,
                    error_type=type(exc).__name__,
                )
                response = JSONResponse(
                    status_code=500,
                    content=error_body(
                        ErrorCode.INTERNAL_ERROR,
                        "系統發生未預期錯誤，請稍後再試。",
                        rid,
                    ),
                )
            response.headers[REQUEST_ID_HEADER] = rid
            return response
        finally:
            _request_id_var.reset(token)
