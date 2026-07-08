# backend/app/tests/test_error_contract.py
"""
core.errors 單元測試（P9 Phase 2A.3）：AppError、error_body、code_for_status、GONE_CODES。
"""
from __future__ import annotations

from app.core.errors import (
    AppError, ErrorCode, GONE_CODES, error_body, code_for_status,
)


def test_app_error_fields():
    e = AppError(code=ErrorCode.PREVIEW_SHA_MISMATCH, message="mismatch", status_code=409,
                 details={"a": 1})
    assert e.code == "PREVIEW_SHA_MISMATCH"
    assert e.message == "mismatch"
    assert e.status_code == 409
    assert e.details == {"a": 1}


def test_app_error_default_details_empty():
    e = AppError(code=ErrorCode.PREVIEW_NOT_FOUND, message="x", status_code=404)
    assert e.details == {}


def test_error_body_shape():
    b = error_body("PREVIEW_EXPIRED", "過期", "req_123", {"k": "v"})
    assert b == {
        "error": {"code": "PREVIEW_EXPIRED", "message": "過期", "details": {"k": "v"}},
        "request_id": "req_123",
    }


def test_error_body_defaults_details():
    b = error_body("INTERNAL_ERROR", "boom", None)
    assert b["error"]["details"] == {}
    assert b["request_id"] is None


def test_gone_codes_are_the_three_410_states():
    assert GONE_CODES == frozenset({
        ErrorCode.PREVIEW_EXPIRED, ErrorCode.PREVIEW_REVOKED, ErrorCode.PREVIEW_CONSUMED,
    })


def test_code_for_status_mapping():
    assert code_for_status(401) == ErrorCode.AUTH_REQUIRED
    assert code_for_status(403) == ErrorCode.PREVIEW_FORBIDDEN
    assert code_for_status(404) == ErrorCode.PREVIEW_NOT_FOUND
    assert code_for_status(409) == ErrorCode.PREVIEW_SHA_MISMATCH
    assert code_for_status(413) == ErrorCode.PREVIEW_TOO_LARGE
    assert code_for_status(503) == ErrorCode.PREVIEW_STORAGE_UNAVAILABLE


def test_code_for_status_unknown_is_internal():
    assert code_for_status(418) == ErrorCode.INTERNAL_ERROR
