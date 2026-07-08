# backend/app/tests/test_api_preview_errors.py
"""
Preview API 錯誤契約（P9 Phase 2A.3）：machine-readable code + X-Request-ID。

沿用 test_api_preview 的 TestClient + dependency_overrides + monkeypatch 模式；
不碰真 DB。驗每個錯誤路徑回正確 code / status，且 body.request_id == X-Request-ID header。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
import app.services.preview_artifact as pa
from app.security import get_current_user
from app.services.crypto_box import PreviewKeyError

app = main_mod.app
client = TestClient(app)

ADMIN = {"id": 1, "username": "admin", "role": "admin"}
OWNER = {"id": 5, "username": "u5", "role": "user"}
OTHER = {"id": 6, "username": "u6", "role": "user"}


def _meta(**over):
    base = {
        "id": 10, "preview_id": "tok", "filename": "x.xlsx", "ext": "xlsx",
        "sha256_full": "sha", "row_count": 1, "parser_type": "auto",
        "provenance": {"pipeline_version": "P9", "target_id": "t"}, "created_by": 5,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "sealed_at": None,
    }
    base.update(over)
    return base


def _auth(user):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    # 靜音 audit（避免真 DB）
    monkeypatch.setattr("app.api.preview.write_audit", lambda **kw: 1)
    yield
    app.dependency_overrides.clear()


def _assert_contract(r, code, status):
    assert r.status_code == status, r.text
    b = r.json()
    assert b["error"]["code"] == code
    assert "message" in b["error"]
    # X-Request-ID header 與 body.request_id 一致
    assert r.headers.get("X-Request-ID")
    assert b["request_id"] == r.headers["X-Request-ID"]
    return b


# 13/14/15. 410 三態
@pytest.mark.parametrize("state,code", [
    ("expired", "PREVIEW_EXPIRED"),
    ("revoked", "PREVIEW_REVOKED"),
    ("consumed", "PREVIEW_CONSUMED"),
])
def test_gone_states(monkeypatch, state, code):
    _auth(OWNER)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta())
    monkeypatch.setattr(pa, "state_of", lambda meta: state)
    r = client.get("/api/preview/tok")
    _assert_contract(r, code, 410)


# 16. not found
def test_not_found(monkeypatch):
    _auth(OWNER)
    monkeypatch.setattr(pa, "get_meta", lambda pid: None)
    r = client.get("/api/preview/nope")
    _assert_contract(r, "PREVIEW_NOT_FOUND", 404)


# 17. forbidden（非 owner 非 admin）
def test_forbidden(monkeypatch):
    _auth(OTHER)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=5))
    r = client.get("/api/preview/tok")
    _assert_contract(r, "PREVIEW_FORBIDDEN", 403)


# 18. sha mismatch（save 路徑）
def test_sha_mismatch(monkeypatch):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=1))
    monkeypatch.setattr(pa, "state_of", lambda meta: "active")
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"rawbytes")
    monkeypatch.setattr(pa, "sha256_hex", lambda raw: "DIFFERENT")
    r = client.post("/api/preview/tok/save", json={"project_id": "P1", "target_id": "t"})
    b = _assert_contract(r, "PREVIEW_SHA_MISMATCH", 409)
    # 不洩漏實際 sha
    assert "sha" not in str(b["error"].get("details", {}))


# 19. too large
def test_too_large(monkeypatch):
    _auth(ADMIN)
    monkeypatch.setenv("PREVIEW_MAX_MB", "0")
    r = client.post("/api/preview", files={"file": ("x.xlsx", b"abcdef", "application/octet-stream")})
    _assert_contract(r, "PREVIEW_TOO_LARGE", 413)


# 20. missing key
def test_missing_key(monkeypatch):
    _auth(ADMIN)
    monkeypatch.delenv("PREVIEW_DB_MAX_MB", raising=False)
    monkeypatch.delenv("PREVIEW_MAX_MB", raising=False)
    monkeypatch.setattr("app.api.preview.parse_file_only", lambda *a, **k: [])

    def _raise(**k):
        raise PreviewKeyError("no key")
    monkeypatch.setattr(pa, "create", _raise)
    r = client.post("/api/preview", files={"file": ("x.xlsx", b"abc", "application/octet-stream")})
    _assert_contract(r, "PREVIEW_KEY_MISSING", 503)


# 21. 解析失敗（diagnosis）→ PREVIEW_PARSE_FAILED 422 + diagnosis 於 details
def test_parse_failed_diagnosis(monkeypatch):
    _auth(ADMIN)
    from app.services.ingest import ParseDiagnosisError

    def _raise(*a, **k):
        raise ParseDiagnosisError("no", diagnosis={"available_columns": ["A"]})
    monkeypatch.setattr("app.api.preview.parse_file_only", _raise)
    r = client.post("/api/preview", files={"file": ("x.xlsx", b"abc", "application/octet-stream")})
    b = _assert_contract(r, "PREVIEW_PARSE_FAILED", 422)
    assert b["error"]["details"]["diagnosis"]["available_columns"] == ["A"]


# 21b. 錯誤 body 不含敏感資訊（不含 stack trace / SQL / token）
def test_error_body_no_sensitive(monkeypatch):
    _auth(OWNER)
    monkeypatch.setattr(pa, "get_meta", lambda pid: None)
    r = client.get("/api/preview/nope")
    raw = r.text
    for bad in ("Traceback", "SELECT", "Bearer", "PREVIEW_ARTIFACT_KEY", "postgresql://"):
        assert bad not in raw
