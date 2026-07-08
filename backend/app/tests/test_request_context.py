# backend/app/tests/test_request_context.py
"""
Request ID middleware + 例外邊界（P9 Phase 2A.3）。

用獨立小 app（掛 RequestContextMiddleware + register_error_handlers）驗行為，
不依賴 main 的真實路由 / DB。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.request_context import RequestContextMiddleware, get_request_id
from app.core.error_handlers import register_error_handlers
from app.core.errors import AppError, ErrorCode


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    @app.get("/ok")
    def ok():
        return {"rid_seen": get_request_id()}

    @app.get("/app-error")
    def app_error():
        raise AppError(code=ErrorCode.PREVIEW_EXPIRED, message="expired!", status_code=410,
                       details={"hint": "reupload"})

    @app.get("/boom")
    def boom():
        raise ValueError("secret internal detail SHOULD-NOT-LEAK")

    @app.get("/api/preview/boom")
    def preview_http():
        raise HTTPException(status_code=404, detail="preview 不存在")

    @app.get("/plain-http")
    def plain_http():
        raise HTTPException(status_code=403, detail="forbidden-plain")

    @app.get("/needs-param")
    def needs_param(n: int):     # 缺 n → FastAPI 422 validation
        return {"n": n}

    return app


client = TestClient(_make_app())


# 1 & 2. 無 header → 自動產生，且 response 有 X-Request-ID
def test_generates_request_id_when_absent():
    r = client.get("/ok")
    rid = r.headers.get("X-Request-ID")
    assert rid and rid.startswith("req_")
    assert r.json()["rid_seen"] == rid   # API 內取到的與 header 一致


# 3. 合法 client request id 可沿用
def test_valid_client_id_preserved():
    r = client.get("/ok", headers={"X-Request-ID": "trace-abc_123"})
    assert r.headers["X-Request-ID"] == "trace-abc_123"
    assert r.json()["rid_seen"] == "trace-abc_123"


# 4. 非法 / 過長 id 被替換
@pytest.mark.parametrize("bad", ["has space", "x" * 65, "inject\nnewline", "semi;colon"])
def test_invalid_client_id_replaced(bad):
    r = client.get("/ok", headers={"X-Request-ID": bad})
    got = r.headers["X-Request-ID"]
    assert got.startswith("req_") and got != bad


# 5. concurrent / 連續 request 不互相污染 + context 用後即清
def test_no_cross_request_pollution():
    ids = set()
    for i in range(5):
        r = client.get("/ok", headers={"X-Request-ID": f"cid-{i}"})
        assert r.json()["rid_seen"] == f"cid-{i}"
        ids.add(r.headers["X-Request-ID"])
    assert len(ids) == 5
    assert get_request_id() is None   # request 結束後 context 已清


# 6. exception response 仍有 request_id（header 與 body 一致）
def test_exception_response_has_request_id():
    r = client.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["request_id"] == r.headers["X-Request-ID"]


# 7 & 8. AppError → code/message/status/details 正確
def test_app_error_contract():
    r = client.get("/app-error")
    assert r.status_code == 410
    b = r.json()
    assert b["error"]["code"] == "PREVIEW_EXPIRED"
    assert b["error"]["message"] == "expired!"
    assert b["error"]["details"] == {"hint": "reupload"}
    assert b["request_id"].startswith("req_") or "-" in b["request_id"]


# 9 & 10. 非預期 exception → INTERNAL_ERROR，不洩漏 stack trace / 內部訊息
def test_unhandled_exception_internal_error_no_leak():
    r = client.get("/boom")
    assert r.status_code == 500
    b = r.json()
    assert b["error"]["code"] == "INTERNAL_ERROR"
    raw = r.text
    assert "SHOULD-NOT-LEAK" not in raw
    assert "Traceback" not in raw and "ValueError" not in raw
    assert "line " not in raw


# 11. validation error 不被錯誤轉成 generic 500
def test_validation_error_stays_422():
    r = client.get("/needs-param")    # 缺 n
    assert r.status_code == 422
    # FastAPI 預設 validation 形狀（detail list），非我們的 error contract
    assert "detail" in r.json()


# 12. 非 preview 路徑的 HTTPException 維持預設語意
def test_plain_http_exception_preserved():
    r = client.get("/plain-http")
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden-plain"}


# 12b. preview 路徑的 HTTPException 轉成 error contract（AUTH/NOT_FOUND 等）
def test_preview_http_exception_reshaped():
    r = client.get("/api/preview/boom")
    assert r.status_code == 404
    b = r.json()
    assert b["error"]["code"] == "PREVIEW_NOT_FOUND"
    assert b["request_id"] == r.headers["X-Request-ID"]


# ── §二 全域未預期 500 契約補測 ──────────────────────────────
# 1. 非 preview endpoint 的未預期 exception → INTERNAL_ERROR contract
def test_non_preview_unhandled_is_internal_error():
    r = client.get("/boom")   # 非 /api/preview 路徑
    assert r.status_code == 500
    b = r.json()
    assert b["error"]["code"] == "INTERNAL_ERROR"
    assert b["error"]["message"] == "系統發生未預期錯誤，請稍後再試。"
    assert b["error"]["details"] == {}


# 2 & 3. 500 沿用 client 合法 X-Request-ID；body.request_id == header
def test_500_reuses_valid_client_request_id():
    r = client.get("/boom", headers={"X-Request-ID": "op-trace-42"})
    assert r.status_code == 500
    assert r.headers["X-Request-ID"] == "op-trace-42"
    assert r.json()["request_id"] == "op-trace-42"


# 4. 非法 X-Request-ID 在 500 路徑也被替換
def test_500_invalid_client_request_id_replaced():
    r = client.get("/boom", headers={"X-Request-ID": "bad id with spaces"})
    assert r.status_code == 500
    rid = r.headers["X-Request-ID"]
    assert rid.startswith("req_") and rid != "bad id with spaces"
    assert r.json()["request_id"] == rid


# 7. 未知 route 的 404 不被轉成 INTERNAL_ERROR（維持 FastAPI 預設）
def test_404_route_not_converted():
    r = client.get("/definitely-not-a-route")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


# 8. structured log 不含 exception 的敏感 message，只記 error_type
def test_unhandled_log_has_no_sensitive_message(caplog):
    import json
    import logging
    with caplog.at_level(logging.ERROR, logger="celltrail"):
        client.get("/boom")   # raises ValueError("secret internal detail SHOULD-NOT-LEAK")
    evts = []
    for rec in caplog.records:
        if rec.name == "celltrail":
            try:
                evts.append(json.loads(rec.getMessage()))
            except Exception:
                pass
    unhandled = [e for e in evts if e.get("event") == "request.unhandled_exception"]
    assert len(unhandled) >= 1
    e = unhandled[-1]
    assert e["error_type"] == "ValueError"
    assert "SHOULD-NOT-LEAK" not in json.dumps(e)   # 原始 message 不入 log


# 9. 連續 500 request 的 request_id 不互相污染 + context 用後即清
def test_concurrent_500_request_id_isolation():
    ids = []
    for i in range(4):
        r = client.get("/boom", headers={"X-Request-ID": f"e500-{i}"})
        assert r.json()["request_id"] == f"e500-{i}"
        ids.append(r.headers["X-Request-ID"])
    assert ids == [f"e500-{i}" for i in range(4)]
    assert get_request_id() is None   # request 結束後 context 已清
