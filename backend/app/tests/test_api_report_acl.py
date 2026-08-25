# backend/app/tests/test_api_report_acl.py
"""
證物報告的存取邊界（2026-08-25 決策後）。

背景 —— 這條規格與實作矛盾了一個多月：
  `evidence_report` 原本掛 `require_admin` **並且** 呼叫
  `assert_project_access(viewer)`，兩者都要過 → **專案 owner 也下載不了自己案件的報告**，
  只有系統 admin 能出。但同一支函式的 docstring 一直寫「需 viewer 以上」。
  P9 Phase 2A 把它列為 REPORT_ACL_SPEC_MISMATCH 而未逕行修改，因為那是產品決策。

決策：改採 **該專案 viewer 以上**。偵查實務上承辦人要為自己的案件出報告，
每份都找系統管理員代勞不合理；而報告內容本就是該案成員在系統內看得到的資料，
不因匯出成 PDF 而變得更敏感。

本檔守的是「放寬之後，界線仍然在」—— 非該專案成員必須擋，未登入必須擋。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_mod
from app.security import get_current_user

app = main_mod.app
client = TestClient(app)

ADMIN = {"id": 1, "username": "admin1", "role": "admin"}
MEMBER = {"id": 5, "username": "u5", "role": "user"}
OUTSIDER = {"id": 9, "username": "u9", "role": "user"}
URL = "/api/projects/case-1/evidence-report"


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr("app.api.report.build_evidence_report",
                        lambda **kw: b"%PDF-1.4 fake")
    monkeypatch.setattr("app.api.report.write_audit", lambda **kw: 1)
    yield
    app.dependency_overrides.clear()


def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user


def _allow_access(monkeypatch):
    monkeypatch.setattr("app.api.report.assert_project_access", lambda u, p, m: None)


def _deny_access(monkeypatch):
    def _deny(u, p, m):
        raise HTTPException(status_code=403, detail="無此案件的存取權限")
    monkeypatch.setattr("app.api.report.assert_project_access", _deny)


def test_project_member_can_export(monkeypatch):
    """核心決策：專案成員（viewer+）現在出得了報告 —— 這正是本次改動要達成的事。"""
    _as(MEMBER); _allow_access(monkeypatch)
    r = client.get(URL)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_non_member_is_still_blocked(monkeypatch):
    """放寬的是「不必是系統 admin」，不是「誰都能拿」。非該專案成員仍必須 403。"""
    _as(OUTSIDER); _deny_access(monkeypatch)
    assert client.get(URL).status_code == 403


def test_system_admin_can_still_export(monkeypatch):
    """回歸：改動前只有 admin 能出，改動後 admin 仍須能出。"""
    _as(ADMIN); _allow_access(monkeypatch)
    assert client.get(URL).status_code == 200


def test_anonymous_is_rejected():
    """未登入一律 401 —— 報告含完整 audit 時間軸與 SHA-256，不是公開資料。"""
    app.dependency_overrides.clear()
    assert client.get(URL).status_code == 401


def test_export_writes_audit(monkeypatch):
    """誰在何時匯出必須留紀錄 —— 這是放寬權限後唯一的事後追溯手段。"""
    calls = []
    monkeypatch.setattr("app.api.report.write_audit", lambda **kw: calls.append(kw) or 1)
    _as(MEMBER); _allow_access(monkeypatch)
    assert client.get(URL).status_code == 200
    actions = [c.get("action") for c in calls]
    assert "export_report" in actions, actions


def test_endpoint_no_longer_depends_on_require_admin():
    """釘住實作：若日後有人把 require_admin 加回去，專案成員又會被擋，
    而症狀只是「下載鈕沒反應」—— 很難聯想到權限守衛。"""
    import inspect
    import app.api.report as rp
    src = inspect.getsource(rp.evidence_report)
    deco = inspect.getsource(rp)[:inspect.getsource(rp).index("def evidence_report")]
    assert "require_admin" not in deco.split("@router.get")[-1], "端點不應再掛 require_admin"
    assert "assert_project_access" in src, "但專案層級的存取檢查必須保留"
