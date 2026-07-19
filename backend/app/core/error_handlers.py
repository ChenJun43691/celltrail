# backend/app/core/error_handlers.py
"""
Exception handler 註冊（P9 Phase 2A.3；OBS-ERR-NO-STRUCTURED-LOG 修補 2026-07）。

main.py 只呼叫 register_error_handlers(app)；實作集中在此。

範圍：
  - AppError → 統一 error contract（全域；只有 preview 路徑會 raise AppError）。
    **每個 AppError 都會 emit 一筆 structured JSON log**（4xx=WARNING app.error.client、
    5xx=ERROR app.error.server），讓客戶端拿到的 request_id 能在 server log 反查。
    這是 OBS-ERR-NO-STRUCTURED-LOG 的修補點：修補前 AppError 只回 JSON、不落 log，
    錯誤情境（410/403/404…）在 production 只剩 uvicorn access log 一行、無法 request_id 反查。
  - preview 路徑上的 Starlette/FastAPI HTTPException → 轉成 error contract
    （處理 get_current_user 的 401、assert_project_access 的 403 等 dependency 例外）。
  - 其餘路徑的 HTTPException、RequestValidationError（422）維持 FastAPI 預設語意，零回歸。
  - 未預期例外（真正的 500）不在此處理，由 RequestContextMiddleware 就地攔截，
    以保證 request_id 與 X-Request-ID header（見 request_context.py 註解）。

logging 只記「HTTP 契約層」欄位（status_code / error_code / method / route）；
**不記** message 原文、details、path 上的動態識別碼（preview_id / token / uuid）、
query string、body、header。route 一律用 route template（見 _route_template）。

duplicate logging 策略（見 §requirement 8）：
  - 全域 handler 負責「每個 AppError 的契約層 log」（此檔）。
  - API 層（preview.py）**不再**為 AppError 重複 log 契約層資訊；只保留「記錄非 AppError
    根因」的 domain event（例如 read rebuild 失敗時捕捉的底層 exception type，全域 handler
    看不到那個非 AppError 例外，故該筆 domain log 是額外資訊、非重複）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, code_for_status, error_body
from app.core.request_context import get_request_id
from app.core import logging_utils as log

_PREVIEW_PATH_PREFIX = "/api/preview"


def _detail_message(exc: StarletteHTTPException) -> str:
    d = exc.detail
    return d if isinstance(d, str) else "請求無法處理"


def _route_template(request: Request) -> str:
    """回傳 route template（如 /api/preview/{preview_id}/save），**絕不含動態識別碼**。

    優先序：
      1. matched route 的 .path（FastAPI APIRoute template）—— AppError 一定由已 match 的
         endpoint raise，故此值在實務上必存在。
      2. fallback：把 request path 中的 path_params 具體值換回 {key}（避免記到 preview_id）。
      3. 最終 fallback：安全佔位字串（不落原始 path）。
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None) or getattr(route, "path_format", None)
    if template:
        return template

    # fallback：用 path_params 把具體值抽換掉，確保不落動態識別碼。
    raw = request.scope.get("path") or ""
    params: Dict[str, Any] = request.scope.get("path_params") or {}
    if raw and params:
        for k, v in params.items():
            if v is None:
                continue
            sv = str(v)
            if sv:
                raw = raw.replace(sv, "{" + k + "}")
        return raw
    # 無 route 也無 path_params：不確定 path 是否夾帶識別碼 → 不落原始 path。
    return "<unresolved-route>"


def _log_app_error(request: Request, exc: AppError) -> None:
    """為每個 AppError emit 一筆契約層 structured log（request_id 由 logging_utils 自動帶入）。

    只記安全欄位；不記 message 原文 / details / 動態 path / query / body / header。
    """
    is_server = exc.status_code >= 500
    fields = {
        "status_code": exc.status_code,
        "error_code": exc.code,
        "method": request.method,
        "route": _route_template(request),
    }
    if is_server:
        log.log_error("app.error.server", **fields)
    else:
        log.log_warning("app.error.client", **fields)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        # 先落 log（契約層、可 request_id 反查），再回原封不動的 response contract。
        _log_app_error(request, exc)
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
