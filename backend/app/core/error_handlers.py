# backend/app/core/error_handlers.py
"""
Exception handler 註冊（P9 Phase 2A.3）。

main.py 只呼叫 register_error_handlers(app)；實作集中在此。

範圍：
  - AppError → 統一 error contract（全域；只有 preview 路徑會 raise AppError）。
  - preview 路徑上的 Starlette/FastAPI HTTPException → 轉成 error contract
    （處理 get_current_user 的 401、assert_project_access 的 403 等 dependency 例外）。
  - 其餘路徑的 HTTPException、RequestValidationError（422）維持 FastAPI 預設語意，零回歸。
  - 未預期例外（真正的 500）不在此處理，由 RequestContextMiddleware 就地攔截，
    以保證 request_id 與 X-Request-ID header（見 request_context.py 註解）。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, code_for_status, error_body
from app.core.request_context import get_request_id

_PREVIEW_PATH_PREFIX = "/api/preview"


def _detail_message(exc: StarletteHTTPException) -> str:
    d = exc.detail
    return d if isinstance(d, str) else "請求無法處理"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, get_request_id(), exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        # 只 reshape preview 路徑；其餘維持 FastAPI 預設 {"detail": ...}，零回歸。
        if request.url.path.startswith(_PREVIEW_PATH_PREFIX):
            return JSONResponse(
                status_code=exc.status_code,
                content=error_body(
                    code_for_status(exc.status_code),
                    _detail_message(exc),
                    get_request_id(),
                ),
                headers=getattr(exc, "headers", None) or None,
            )
        # 非 preview：沿用 FastAPI 預設行為。
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or None,
        )
