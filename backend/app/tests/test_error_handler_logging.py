# backend/app/tests/test_error_handler_logging.py
"""
全域 AppError handler 的 structured logging（OBS-ERR-NO-STRUCTURED-LOG 修補，2026-07）。

目標：每個 AppError 錯誤路徑都 emit 一筆可用 request_id 反查的 structured JSON log，
且 HTTP response contract 完全不變、log 不含敏感資料 / 動態 preview_id / query string。

用獨立小 app（掛 RequestContextMiddleware + register_error_handlers）驗行為，
不依賴 main 的真實路由 / DB —— 與 test_request_context 同模式。
"""
from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.request_context import RequestContextMiddleware, get_request_id
from app.core.error_handlers import register_error_handlers, _route_template
from app.core.errors import AppError, ErrorCode


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    # 動態識別碼在 path 上：驗 route template 不落原始 id。
    @app.get("/api/preview/{preview_id}")
    def gone(preview_id: str, code: str = "PREVIEW_CONSUMED", status: int = 410):
        raise AppError(code=code, message="狀態訊息（不應入 log）", status_code=status,
                       details={"secret_hint": "SHOULD-NOT-LEAK-IN-LOG"})

    @app.post("/api/preview/{preview_id}/save")
    def save(preview_id: str):
        raise AppError(code=ErrorCode.PREVIEW_REVOKED, message="已撤銷", status_code=410)

    @app.post("/api/preview")
    def create():
        raise AppError(code=ErrorCode.PREVIEW_TOO_LARGE, message="太大", status_code=413)

    @app.get("/api/preview/{preview_id}/forbidden")
    def forbidden(preview_id: str):
        raise AppError(code=ErrorCode.PREVIEW_FORBIDDEN, message="無權", status_code=403)

    @app.get("/api/preview/{preview_id}/missing")
    def missing(preview_id: str):
        raise AppError(code=ErrorCode.PREVIEW_NOT_FOUND, message="找不到", status_code=404)

    @app.get("/api/preview/{preview_id}/badparse")
    def badparse(preview_id: str):
        raise AppError(code=ErrorCode.PREVIEW_PARSE_FAILED, message="解析失敗", status_code=422)

    # AppError 500（不是未預期 exception，是刻意的 server-side AppError）。
    @app.get("/api/preview/{preview_id}/servererr")
    def servererr(preview_id: str):
        raise AppError(code=ErrorCode.PREVIEW_PARSE_FAILED,
                       message="重建失敗 internal SHOULD-NOT-LEAK", status_code=500,
                       details={"token": "Bearer eyJsecret", "password": "p@ss"})

    # details 內含敏感值（驗 body/ log 皆不外洩敏感 details 到 log）。
    @app.get("/api/preview/{preview_id}/leaky")
    def leaky(preview_id: str):
        raise AppError(code=ErrorCode.PREVIEW_FORBIDDEN, message="x", status_code=403,
                       details={"authorization": "Bearer eyJxxx", "raw": "AKIA-secret"})

    return app


client = TestClient(_make_app(), raise_server_exceptions=False)


def _celltrail_events(caplog):
    out = []
    for rec in caplog.records:
        if rec.name != "celltrail":
            continue
        try:
            out.append(json.loads(rec.getMessage()))
        except Exception:
            pass
    return out


def _last_error_event(caplog):
    evts = [e for e in _celltrail_events(caplog)
            if e.get("event") in ("app.error.client", "app.error.server")]
    assert evts, "no app.error.* structured log emitted"
    return evts[-1]


def _assert_contract_unchanged(r, code, status):
    """response contract：status / error.code / error.message / request_id / X-Request-ID。"""
    assert r.status_code == status, r.text
    b = r.json()
    assert b["error"]["code"] == code
    assert isinstance(b["error"]["message"], str) and b["error"]["message"]
    assert "details" in b["error"]
    assert r.headers.get("X-Request-ID")
    assert b["request_id"] == r.headers["X-Request-ID"]
    return b


# ── A. 410 PREVIEW_CONSUMED ─────────────────────────────────
def test_A_consumed_contract_and_warning_log(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        r = client.get("/api/preview/SECRET_PID_abcdef123456",
                       params={"code": "PREVIEW_CONSUMED", "status": 410})
    b = _assert_contract_unchanged(r, "PREVIEW_CONSUMED", 410)
    e = _last_error_event(caplog)
    assert e["level"] == "WARNING"
    assert e["event"] == "app.error.client"
    assert e["status_code"] == 410
    assert e["error_code"] == "PREVIEW_CONSUMED"
    assert e["method"] == "GET"
    # route 為 template、不含具體 preview_id
    assert e["route"] == "/api/preview/{preview_id}"
    assert "SECRET_PID_abcdef123456" not in json.dumps(e)
    # request_id 與 response header/body 一致
    assert e["request_id"] == r.headers["X-Request-ID"] == b["request_id"]


# ── B. 410 PREVIEW_REVOKED（save 路徑）───────────────────────
def test_B_revoked_warning_log(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        r = client.post("/api/preview/PID_revoke_9876543210/save")
    _assert_contract_unchanged(r, "PREVIEW_REVOKED", 410)
    e = _last_error_event(caplog)
    assert e["level"] == "WARNING"
    assert e["event"] == "app.error.client"
    assert e["status_code"] == 410
    assert e["error_code"] == "PREVIEW_REVOKED"
    assert e["method"] == "POST"
    assert e["route"] == "/api/preview/{preview_id}/save"
    assert "PID_revoke_9876543210" not in json.dumps(e)


# ── C. 403 / 404 / 413 / 422 均為 app.error.client ──────────
@pytest.mark.parametrize("path,method,code,status,route", [
    ("/api/preview/PID_forbid_xxxxxx/forbidden", "get", "PREVIEW_FORBIDDEN", 403,
     "/api/preview/{preview_id}/forbidden"),
    ("/api/preview/PID_missing_yyyyy/missing", "get", "PREVIEW_NOT_FOUND", 404,
     "/api/preview/{preview_id}/missing"),
    ("/api/preview", "post", "PREVIEW_TOO_LARGE", 413, "/api/preview"),
    ("/api/preview/PID_parse_zzzzzz/badparse", "get", "PREVIEW_PARSE_FAILED", 422,
     "/api/preview/{preview_id}/badparse"),
])
def test_C_client_errors(caplog, path, method, code, status, route):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        r = getattr(client, method)(path)
    b = _assert_contract_unchanged(r, code, status)
    e = _last_error_event(caplog)
    assert e["event"] == "app.error.client"
    assert e["level"] == "WARNING"
    assert e["error_code"] == code
    assert e["status_code"] == status
    assert e["method"] == method.upper()
    assert e["route"] == route
    assert e["request_id"] == b["request_id"]


# ── D. AppError 500 → app.error.server / ERROR，無洩漏 ────────
def test_D_app_error_500_server_log_no_leak(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        r = client.get("/api/preview/PID_500_abcdef12345/servererr")
    # response 契約
    assert r.status_code == 500
    b = r.json()
    assert b["error"]["code"] == "PREVIEW_PARSE_FAILED"
    # response 不洩漏 stack trace（AppError message 會回給 client，但不含 traceback）
    assert "Traceback" not in r.text
    # structured log
    e = _last_error_event(caplog)
    assert e["event"] == "app.error.server"
    assert e["level"] == "ERROR"
    assert e["status_code"] == 500
    assert e["error_code"] == "PREVIEW_PARSE_FAILED"
    dumped = json.dumps(e)
    # log 不含 message 原文、details 的敏感值、stack trace
    assert "SHOULD-NOT-LEAK" not in dumped
    assert "Bearer" not in dumped and "eyJsecret" not in dumped
    assert "p@ss" not in dumped
    assert "Traceback" not in dumped


# ── E. 安全：details/query/token 不進 log；preview_id 不進 log ──
def test_E_details_not_logged(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        r = client.get("/api/preview/PID_leaky_aaaaaaaa/leaky")
    assert r.status_code == 403
    e = _last_error_event(caplog)
    dumped = json.dumps(e)
    # AppError.details 完整內容不進 log
    assert "authorization" not in e
    assert "raw" not in e
    assert "Bearer" not in dumped
    assert "AKIA-secret" not in dumped
    # 只有 allowlist 欄位
    assert set(e.keys()) == {
        "timestamp", "level", "event", "request_id",
        "status_code", "error_code", "method", "route",
    }


def test_E_query_string_not_logged(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        # query string 含 token；route template 不含 query，log 也不得含。
        r = client.get(
            "/api/preview/PID_q_bbbbbbbbbb",
            params={"code": "PREVIEW_CONSUMED", "status": 410,
                    "access_token": "eyJqueryTOKEN", "jwt": "SECRETJWT"},
        )
    assert r.status_code == 410
    e = _last_error_event(caplog)
    dumped = json.dumps(e)
    assert "eyJqueryTOKEN" not in dumped
    assert "SECRETJWT" not in dumped
    assert "access_token" not in dumped
    # route 仍是 template，無 query
    assert e["route"] == "/api/preview/{preview_id}"
    assert "?" not in e["route"]


def test_E_preview_id_never_in_structured_log(caplog):
    pid = "SYNTH_pv_AAAABBBBCCCCDDDD-1234EXAMPLE"  # 合成、同真實 preview_id 形狀（含 dash/混大小寫）
    with caplog.at_level(logging.INFO, logger="celltrail"):
        client.get(f"/api/preview/{pid}", params={"status": 410})
    for e in _celltrail_events(caplog):
        assert pid not in json.dumps(e)


# ── F. Request context ─────────────────────────────────────
def test_F_client_request_id_consistent_across_log_body_header(caplog):
    cid = "trace-e2e_20260719"
    with caplog.at_level(logging.INFO, logger="celltrail"):
        r = client.get("/api/preview/PID_ctx_cccccccc",
                       params={"status": 410}, headers={"X-Request-ID": cid})
    assert r.headers["X-Request-ID"] == cid
    assert r.json()["request_id"] == cid
    e = _last_error_event(caplog)
    assert e["request_id"] == cid   # log 三者一致


def test_F_no_cross_request_pollution(caplog):
    seen = []
    with caplog.at_level(logging.INFO, logger="celltrail"):
        for i in range(5):
            cid = f"iso-{i}"
            r = client.get(f"/api/preview/PID_iso_{i}0000000",
                           params={"status": 410}, headers={"X-Request-ID": cid})
            assert r.json()["request_id"] == cid
            seen.append(cid)
    # 每個 error log 的 request_id 對得上、彼此不污染
    err = [e for e in _celltrail_events(caplog)
           if e.get("event") == "app.error.client"]
    got = [e["request_id"] for e in err]
    for cid in seen:
        assert cid in got
    assert get_request_id() is None   # request 結束後 context 已清


# ── route template helper 單元 + fallback ───────────────────
def test_route_template_prefers_route_path():
    class _FakeReq:
        def __init__(self, scope):
            self.scope = scope
    class _Route:
        path = "/api/preview/{preview_id}"
    req = _FakeReq({"route": _Route(), "path": "/api/preview/REALID", "path_params": {}})
    assert _route_template(req) == "/api/preview/{preview_id}"


def test_route_template_fallback_substitutes_path_params():
    class _FakeReq:
        def __init__(self, scope):
            self.scope = scope
    # 無 route object → 用 path_params 抽換具體值
    req = _FakeReq({"route": None, "path": "/api/preview/REALSECRET/save",
                    "path_params": {"preview_id": "REALSECRET"}})
    out = _route_template(req)
    assert out == "/api/preview/{preview_id}/save"
    assert "REALSECRET" not in out


def test_route_template_final_fallback_no_raw_path():
    class _FakeReq:
        def __init__(self, scope):
            self.scope = scope
    # 無 route、無 path_params → 不落原始 path（可能夾帶識別碼）
    req = _FakeReq({"route": None, "path": "/api/preview/UNKNOWN_ID", "path_params": {}})
    assert _route_template(req) == "<unresolved-route>"


# ── duplicate logging：sha_mismatch 只剩全域一筆（非兩筆）──────
def test_no_duplicate_log_for_single_app_error(caplog):
    with caplog.at_level(logging.INFO, logger="celltrail"):
        client.post("/api/preview/PID_dup_dddddddddd/save")  # → PREVIEW_REVOKED 410
    err = [e for e in _celltrail_events(caplog)
           if e.get("event") in ("app.error.client", "app.error.server")]
    # 單一 AppError → 剛好一筆契約層 log（不重複）
    assert len(err) == 1
