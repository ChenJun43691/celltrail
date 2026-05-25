"""
format_reports API 業務邏輯測試（2026-05-25）

WAKE_UP_TODO #2 最後一塊。format_reports 既有 P3–P7 契約測試只驗端點
有掛、未驗以下行為：

  • create_report 訪客（current_user=None）→ reporter_user_id=None
  • create_report 已登入 → reporter_user_id=user.id
  • create_report 匿名 admin (id=0) → reporter_user_id=NULL（FK 容錯）
  • create_report request.client=None → reporter_ip=None
  • update_report 404 路徑
  • update_report 成功路徑寫 audit + status/note 進 details
  • update_report 匿名 admin → handled_by=NULL（FK 容錯）

本檔同時守護同 commit 修的 anonymous admin FK guard（與
grant_member / delete_project 同款處理）。

不依賴 DB：FakeConn 攔 SQL；write_audit 攔成 spy。
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# ─────────────────────────────────────────────────────────────
# Fake infra
# ─────────────────────────────────────────────────────────────
class _ScriptedCursor:
    def __init__(self, captured: list, fetch_results: list):
        self._captured = captured
        self._fetches = list(fetch_results)

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=None, *, prepare=None):
        self._captured.append({"sql": sql, "params": params, "prepare": prepare})
    def fetchone(self):
        return self._fetches.pop(0) if self._fetches else None


class _ScriptedConn:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self): return self._cursor


def _install_scripted_conn(monkeypatch, captured, fetch_results):
    cur = _ScriptedCursor(captured, fetch_results)
    @contextmanager
    def fake_get_conn(): yield _ScriptedConn(cur)
    import app.api.format_reports as fr
    monkeypatch.setattr(fr, "get_conn", fake_get_conn)


@pytest.fixture(autouse=True)
def _disable_limiter(monkeypatch):
    """
    @limiter.limit("10/hour") 在 decorator 層用 isinstance(request, starlette.Request)
    檢查，本檔用 _FakeRequest 直接呼叫函式會被擋。把 limiter 整體 disable 即可
    繞過該 check（rate-limit 行為本身不屬於業務邏輯，另由 slowapi 測試覆蓋）。
    """
    import app.services.limiter as lm
    monkeypatch.setattr(lm.limiter, "enabled", False)


def _spy_write_audit(monkeypatch):
    calls: list[dict] = []
    def spy(**kwargs):
        calls.append(kwargs)
        return 999
    import app.api.format_reports as fr
    monkeypatch.setattr(fr, "write_audit", spy)
    return calls


class _FakeClient:
    def __init__(self, host: str): self.host = host


class _FakeRequest:
    """簡化版 Request：只有 client.host 和 headers 兩個屬性。"""
    def __init__(self, client_host: str | None = "127.0.0.1", headers: dict | None = None):
        self.client = _FakeClient(client_host) if client_host else None
        self.headers = headers or {}


from datetime import datetime, timezone

_FAKE_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────
# A. create_report — reporter 身份組合
# ─────────────────────────────────────────────────────────────
def test_create_report_anonymous_visitor(monkeypatch):
    """訪客（current_user=None）→ reporter_user_id=NULL，IP 仍記。"""
    import app.api.format_reports as fr
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[(101, _FAKE_NOW)])

    payload = fr.FormatReportIn(filename="weird.xlsx", headers=["a", "b"], diagnosis={"reason": "no header"})
    result = fr.create_report(
        request=_FakeRequest(client_host="203.0.113.7"),
        payload=payload,
        current_user=None,
    )
    assert result["ok"] is True
    assert result["id"] == 101

    params = captured[0]["params"]
    # (filename, headers_json, diagnosis_json, note, reporter_id, reporter_ip)
    assert params[0] == "weird.xlsx"
    assert params[3] is None         # note
    assert params[4] is None, "訪客 reporter_user_id 應為 NULL"
    assert params[5] == "203.0.113.7"


def test_create_report_logged_in_user(monkeypatch):
    """登入使用者 → reporter_user_id=user.id。"""
    import app.api.format_reports as fr
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[(102, _FAKE_NOW)])

    payload = fr.FormatReportIn(filename="x.xlsx", headers=[], diagnosis={}, note="這份 PDF 是掃描檔")
    fr.create_report(
        request=_FakeRequest(),
        payload=payload,
        current_user={"id": 42, "username": "alice", "role": "user"},
    )
    params = captured[0]["params"]
    assert params[3] == "這份 PDF 是掃描檔"
    assert params[4] == 42


def test_create_report_anonymous_admin_fk_guard(monkeypatch):
    """
    id=0（AUTH_ENABLED=false 的 anonymous admin）→ reporter_user_id=NULL。
    若沒這個 guard，FK reporter_user_id → users.id 會在開發環境上每次回報
    都炸 IntegrityError。對齊 grant_member / delete_project 同款處理。
    """
    import app.api.format_reports as fr
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[(103, _FAKE_NOW)])

    payload = fr.FormatReportIn(filename="x.xlsx", headers=[], diagnosis={})
    fr.create_report(
        request=_FakeRequest(),
        payload=payload,
        current_user={"id": 0, "username": "anonymous", "role": "admin"},
    )
    params = captured[0]["params"]
    assert params[4] is None, "id=0 必須轉成 NULL（FK 容錯）"


def test_create_report_no_client_ip(monkeypatch):
    """request.client=None（背景任務 / 測試）→ reporter_ip=NULL，不該爆 AttributeError。"""
    import app.api.format_reports as fr
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[(104, _FAKE_NOW)])

    payload = fr.FormatReportIn(filename="x.xlsx", headers=[], diagnosis={})
    fr.create_report(
        request=_FakeRequest(client_host=None),
        payload=payload,
        current_user=None,
    )
    params = captured[0]["params"]
    assert params[5] is None


def test_create_report_headers_and_diagnosis_serialized_to_json(monkeypatch):
    """headers / diagnosis 應序列化為 JSON 字串送進 ::jsonb cast。"""
    import app.api.format_reports as fr
    import json as _json
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[(105, _FAKE_NOW)])

    payload = fr.FormatReportIn(
        filename="x.xlsx",
        headers=["欄1", "欄2"],
        diagnosis={"missing": ["start_ts"], "score": 0.7},
    )
    fr.create_report(request=_FakeRequest(), payload=payload, current_user=None)
    params = captured[0]["params"]
    assert _json.loads(params[1]) == ["欄1", "欄2"]
    assert _json.loads(params[2]) == {"missing": ["start_ts"], "score": 0.7}


# ─────────────────────────────────────────────────────────────
# B. update_report — 狀態變更 + audit
# ─────────────────────────────────────────────────────────────
def test_update_report_404_when_not_found(monkeypatch):
    """report_id 不存在 → 404，不寫 audit。"""
    import app.api.format_reports as fr
    _install_scripted_conn(monkeypatch, [], fetch_results=[None])
    audit_calls = _spy_write_audit(monkeypatch)

    payload = fr.HandleIn(status="handled", note="已加入 dialect")
    with pytest.raises(HTTPException) as exc:
        fr.update_report(
            report_id=999, payload=payload,
            request=_FakeRequest(),
            current_admin={"id": 1, "role": "admin", "username": "admin"},
        )
    assert exc.value.status_code == 404
    assert audit_calls == [], "404 應靜默，不寫 audit"


def test_update_report_success_writes_audit(monkeypatch):
    """成功更新 → audit 寫 action=update_format_report，details 含 status+note。"""
    import app.api.format_reports as fr
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[(1, "handled")])
    audit_calls = _spy_write_audit(monkeypatch)

    payload = fr.HandleIn(status="handled", note="已加入 dialect")
    result = fr.update_report(
        report_id=1, payload=payload,
        request=_FakeRequest(),
        current_admin={"id": 1, "role": "admin", "username": "admin"},
    )
    assert result == {"ok": True, "id": 1, "status": "handled"}

    assert len(audit_calls) == 1
    a = audit_calls[0]
    assert a["action"] == "update_format_report"
    assert a["target_type"] == "format_report"
    assert a["target_ref"] == "1"
    assert a["details"] == {"status": "handled", "note": "已加入 dialect"}
    assert a["status_code"] == 200


def test_update_report_anonymous_admin_handled_by_null(monkeypatch):
    """
    id=0（anonymous admin）→ handled_by=NULL（FK 容錯，與 create_report 同款）。
    沒這個 guard，AUTH_ENABLED=false 開發環境 PATCH 會 FK 違反。
    """
    import app.api.format_reports as fr
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[(1, "rejected")])
    _spy_write_audit(monkeypatch)

    payload = fr.HandleIn(status="rejected", note=None)
    fr.update_report(
        report_id=1, payload=payload,
        request=_FakeRequest(),
        current_admin={"id": 0, "role": "admin", "username": "anonymous"},
    )
    # UPDATE params: (status, note, handler_id, status_for_case, report_id)
    params = captured[0]["params"]
    assert params[2] is None, "id=0 必須轉成 NULL（FK 容錯）"


def test_update_report_pydantic_rejects_bad_status():
    """status 必須是 open/handled/rejected 三選一（Pydantic 層的契約）。"""
    import app.api.format_reports as fr
    with pytest.raises(Exception):  # pydantic.ValidationError
        fr.HandleIn(status="resolved", note=None)
