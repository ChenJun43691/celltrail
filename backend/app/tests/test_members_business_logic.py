"""
members API 業務邏輯測試（2026-05-24）

填補 WAKE_UP_TODO #2 剩餘：members（_require_project_owner / grant_member /
revoke_member / delete_project）的業務邏輯沒有專屬測試 —— 已有 P3–P7 契約
測試只驗「端點有掛 + 401 守衛」，沒驗下列關鍵安全合約：

  • _require_project_owner：admin 短路、owner 通過、非 owner 拒絕
  • revoke_member：owner 不能撤自己（避免 project 失去 owner）
                  ；admin 可代撤任何人（含 owner 自己）
  • delete_project：軟刪後寫 audit；rowcount=0 → 404；
                    匿名 admin (id=0) → deleted_by 為 NULL（FK 容錯）
  • grant_member：user 不存在 → 404；is_active=False → 400；
                  匿名 admin → granted_by 為 NULL；
                  expires_at 格式錯 → 400

不依賴真 DB：FakeConn 攔 SQL；write_audit 攔成記錄器 spy 驗 audit 寫入。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# ─────────────────────────────────────────────────────────────
# Fake DB infra：可程式化每次 fetchone() 的回傳
# ─────────────────────────────────────────────────────────────
class _ScriptedCursor:
    """
    fetch_results: list[tuple|None] — 依 fetchone() 呼叫順序 pop。
    captured: list[dict] — 紀錄每次 execute 的 SQL + params + prepare flag。
    rowcount_results: list[int] — 依 execute 順序 pop（用於 UPDATE/DELETE 場景）。
    """
    def __init__(self, captured: list, fetch_results: list, rowcount_results: list):
        self._captured = captured
        self._fetches = list(fetch_results)
        self._rowcounts = list(rowcount_results)
        self.rowcount = 0

    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def execute(self, sql, params=None, *, prepare=None):
        self._captured.append({"sql": sql, "params": params, "prepare": prepare})
        if self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)

    def fetchone(self):
        if not self._fetches:
            return None
        return self._fetches.pop(0)

    def fetchall(self):
        # 把剩餘 fetches 一次倒給 fetchall
        rest = list(self._fetches)
        self._fetches = []
        return rest


class _ScriptedConn:
    def __init__(self, cursor: _ScriptedCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _install_scripted_conn(monkeypatch, mod, captured: list,
                           fetch_results: list, rowcount_results: list | None = None):
    """把 mod.get_conn 換成回傳 _ScriptedConn 的 context manager。"""
    cur = _ScriptedCursor(captured, fetch_results, rowcount_results or [])
    @contextmanager
    def fake_get_conn():
        yield _ScriptedConn(cur)
    monkeypatch.setattr(mod, "get_conn", fake_get_conn)
    return cur


def _spy_write_audit(monkeypatch):
    """把 app.api.members.write_audit 換成 spy；回傳 list[dict] 紀錄呼叫。"""
    calls: list[dict] = []
    def spy(**kwargs):
        calls.append(kwargs)
        return 999  # 假 id
    import app.api.members as members_mod
    monkeypatch.setattr(members_mod, "write_audit", spy)
    return calls


# ─────────────────────────────────────────────────────────────
# A. _require_project_owner
# ─────────────────────────────────────────────────────────────
def test_require_owner_admin_bypasses_db(monkeypatch):
    """admin 短路，不應碰 DB。"""
    @contextmanager
    def explosive():
        raise AssertionError("admin 不該觸發 DB 查詢")
        yield  # pragma: no cover
    import app.api.members as m
    monkeypatch.setattr(m, "get_conn", explosive)

    m._require_project_owner("P-1", {"id": 999, "role": "admin"})  # no raise


def test_require_owner_actual_owner_passes(monkeypatch):
    """role=user + DB 回 ('owner',) → 通過。"""
    import app.api.members as m
    captured = []
    _install_scripted_conn(monkeypatch, m, captured, fetch_results=[("owner",)])
    m._require_project_owner("P-1", {"id": 5, "role": "user"})  # no raise

    assert len(captured) == 1
    # SQL params: (project_id, user_id)
    assert captured[0]["params"] == ("P-1", 5)


def test_require_owner_collaborator_rejected(monkeypatch):
    """role=user + DB 回 ('collaborator',) → 403。"""
    import app.api.members as m
    _install_scripted_conn(monkeypatch, m, [], fetch_results=[("collaborator",)])
    with pytest.raises(HTTPException) as exc:
        m._require_project_owner("P-1", {"id": 5, "role": "user"})
    assert exc.value.status_code == 403
    assert "owner" in exc.value.detail and "admin" in exc.value.detail


def test_require_owner_no_membership_rejected(monkeypatch):
    """role=user + DB 回 None → 403（非成員）。"""
    import app.api.members as m
    _install_scripted_conn(monkeypatch, m, [], fetch_results=[None])
    with pytest.raises(HTTPException) as exc:
        m._require_project_owner("P-1", {"id": 5, "role": "user"})
    assert exc.value.status_code == 403


# ─────────────────────────────────────────────────────────────
# B. revoke_member
# ─────────────────────────────────────────────────────────────
def test_revoke_member_owner_cannot_revoke_self(monkeypatch):
    """
    owner 撤自己 → 400「不能撤銷自己的 owner 授權」。
    這是 project 失去 owner 的最後一道防線（owner 走了就沒人能管理成員）。
    """
    import app.api.members as m
    # _require_project_owner 內查 SELECT permission → ('owner',)
    captured = []
    _install_scripted_conn(monkeypatch, m, captured,
                           fetch_results=[("owner",)])

    with pytest.raises(HTTPException) as exc:
        m.revoke_member(
            project_id="P-1", user_id=5,
            current_user={"id": 5, "role": "user"},
        )
    assert exc.value.status_code == 400
    assert "自己" in exc.value.detail

    # 確認沒走到 DELETE（只跑了 owner check 那一次）
    assert len(captured) == 1
    assert "DELETE" not in captured[0]["sql"].upper()


def test_revoke_member_admin_can_revoke_anyone(monkeypatch):
    """
    admin 可代撤任何人（包含某 owner 的 owner 授權）—— 對 admin 不套用
    self-revoke 防線（admin 不會把自己加進 project_members）。
    """
    import app.api.members as m
    captured = []
    # admin 短路 _require_project_owner（無 DB 查）→ 走 DELETE
    _install_scripted_conn(monkeypatch, m, captured,
                           fetch_results=[], rowcount_results=[1])

    result = m.revoke_member(
        project_id="P-1", user_id=999,
        current_user={"id": 1, "role": "admin"},
    )
    assert result == {"ok": True, "project_id": "P-1", "user_id": 999}
    # 只跑了一次 SQL：DELETE
    assert len(captured) == 1
    assert "DELETE" in captured[0]["sql"].upper()


def test_revoke_member_404_when_no_grant(monkeypatch):
    """DELETE rowcount=0（grant 不存在）→ 404。"""
    import app.api.members as m
    _install_scripted_conn(monkeypatch, m, [],
                           fetch_results=[], rowcount_results=[0])
    with pytest.raises(HTTPException) as exc:
        m.revoke_member(
            project_id="P-1", user_id=999,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 404


# ─────────────────────────────────────────────────────────────
# C. delete_project（軟刪 + audit）
# ─────────────────────────────────────────────────────────────
def test_delete_project_success_writes_audit(monkeypatch):
    """
    UPDATE rowcount=3 → 寫 audit（action='project.delete'）+ 回 affected_rows。
    驗：軟刪 SQL 走 UPDATE deleted_at；audit 含 affected_rows。
    """
    import app.api.members as m
    captured = []
    _install_scripted_conn(monkeypatch, m, captured,
                           fetch_results=[], rowcount_results=[3])
    audit_calls = _spy_write_audit(monkeypatch)

    result = m.delete_project(
        project_id="P-1", request=None,
        current_user={"id": 1, "role": "admin"},
    )
    assert result == {"ok": True, "project_id": "P-1", "affected_rows": 3}

    # 只跑了一次 UPDATE
    assert len(captured) == 1
    sql_upper = captured[0]["sql"].upper()
    assert "UPDATE" in sql_upper and "DELETED_AT" in sql_upper

    # audit 寫了一筆成功記錄
    assert len(audit_calls) == 1
    a = audit_calls[0]
    assert a["action"] == "project.delete"
    assert a["target_ref"] == "P-1"
    assert a["project_id"] == "P-1"
    assert a["details"] == {"affected_rows": 3}
    assert a["status_code"] == 200


def test_delete_project_404_when_already_deleted(monkeypatch):
    """UPDATE rowcount=0（無未刪除 raw_traces）→ 404 + 不寫成功 audit。"""
    import app.api.members as m
    _install_scripted_conn(monkeypatch, m, [],
                           fetch_results=[], rowcount_results=[0])
    audit_calls = _spy_write_audit(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        m.delete_project(
            project_id="P-1", request=None,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 404
    # 不寫成功 audit（404 = 案件不存在，不算「刪除事件」）
    assert audit_calls == [], "404 應靜默，不該記為 delete 事件"


def test_delete_project_db_error_writes_failure_audit(monkeypatch):
    """
    UPDATE 拋例外 → 寫 failure audit (action='project.delete_failed')
    然後回 500。確保失敗事件也有 audit 痕跡（forensic 原則）。
    """
    import app.api.members as m

    # 自製 cursor 在 execute 直接拋
    class _BoomCursor:
        rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, *a, **kw): raise RuntimeError("simulated pg outage")
        def fetchone(self): return None

    class _BoomConn:
        def cursor(self): return _BoomCursor()

    @contextmanager
    def boom_conn():
        yield _BoomConn()
    monkeypatch.setattr(m, "get_conn", boom_conn)

    audit_calls = _spy_write_audit(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        m.delete_project(
            project_id="P-1", request=None,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 500

    # 失敗也寫 audit（action='project.delete_failed'）
    assert len(audit_calls) == 1
    a = audit_calls[0]
    assert a["action"] == "project.delete_failed"
    assert a["status_code"] == 500
    assert "simulated pg outage" in a["error_text"]


def test_delete_project_anonymous_admin_deleter_id_is_null(monkeypatch):
    """
    id=0（anonymous admin，AUTH_ENABLED=false）→ deleted_by 應傳 NULL，
    避免違反 raw_traces.deleted_by → users.id 的 FK（id=0 不在 users 表）。
    """
    import app.api.members as m
    captured = []
    _install_scripted_conn(monkeypatch, m, captured,
                           fetch_results=[], rowcount_results=[1])
    _spy_write_audit(monkeypatch)

    m.delete_project(
        project_id="P-1", request=None,
        current_user={"id": 0, "role": "admin"},
    )
    # UPDATE params 為 (deleter_id, reason, project_id)
    params = captured[0]["params"]
    assert params[0] is None, "id=0 → deleter_id 應為 NULL（FK 容錯）"
    assert params[2] == "P-1"


# ─────────────────────────────────────────────────────────────
# D. grant_member
# ─────────────────────────────────────────────────────────────
def test_grant_member_user_not_found_returns_404(monkeypatch):
    """目標 user 不存在 → 404，不該寫入 project_members。"""
    import app.api.members as m
    captured = []
    # _require_project_owner（admin 短路）+ SELECT user → None
    _install_scripted_conn(monkeypatch, m, captured,
                           fetch_results=[None])

    payload = m.GrantMemberIn(user_id=999, permission="viewer")
    with pytest.raises(HTTPException) as exc:
        m.grant_member(
            project_id="P-1", payload=payload,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 404
    # 只跑 SELECT user，沒走 INSERT
    assert len(captured) == 1
    assert "INSERT" not in captured[0]["sql"].upper()


def test_grant_member_inactive_user_returns_400(monkeypatch):
    """目標 user is_active=False → 400「帳號已停用」。"""
    import app.api.members as m
    _install_scripted_conn(monkeypatch, m, [],
                           # SELECT user → (id, username, is_active=False)
                           fetch_results=[(999, "ghost", False)])

    payload = m.GrantMemberIn(user_id=999, permission="viewer")
    with pytest.raises(HTTPException) as exc:
        m.grant_member(
            project_id="P-1", payload=payload,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 400
    assert "停用" in exc.value.detail


def test_grant_member_bad_expires_at_returns_400(monkeypatch):
    """expires_at 格式錯 → 400「expires_at 格式錯誤」。"""
    import app.api.members as m
    _install_scripted_conn(monkeypatch, m, [],
                           fetch_results=[(999, "alice", True)])

    payload = m.GrantMemberIn(user_id=999, permission="viewer",
                              expires_at="not-an-iso-string")
    with pytest.raises(HTTPException) as exc:
        m.grant_member(
            project_id="P-1", payload=payload,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 400
    assert "expires_at" in exc.value.detail


def test_grant_member_anonymous_admin_granter_id_null(monkeypatch):
    """id=0（anonymous admin）→ granter_id 應傳 NULL（FK 容錯）。"""
    import app.api.members as m
    captured = []
    _install_scripted_conn(monkeypatch, m, captured, fetch_results=[
        (999, "alice", True),                                  # SELECT user
        (1, "viewer", None),                                   # INSERT...RETURNING
    ])

    payload = m.GrantMemberIn(user_id=999, permission="viewer")
    m.grant_member(
        project_id="P-1", payload=payload,
        current_user={"id": 0, "role": "admin"},
    )
    # 第二次 execute = INSERT，params 順序：
    #   (project_id, user_id, permission, expires_at, granted_by)
    insert_params = captured[1]["params"]
    assert insert_params[4] is None, "id=0 → granted_by 應為 NULL"


def test_grant_member_iso_expires_at_parsed(monkeypatch):
    """合法 ISO8601（含 'Z'）→ 解析為 datetime 並傳入 INSERT。"""
    import app.api.members as m
    captured = []
    _install_scripted_conn(monkeypatch, m, captured, fetch_results=[
        (999, "alice", True),
        (1, "collaborator", datetime(2027, 1, 1, tzinfo=timezone.utc)),
    ])

    payload = m.GrantMemberIn(
        user_id=999, permission="collaborator",
        expires_at="2027-01-01T00:00:00Z",
    )
    result = m.grant_member(
        project_id="P-1", payload=payload,
        current_user={"id": 1, "role": "admin"},
    )
    # expires_at 已被 ISO 解析
    insert_params = captured[1]["params"]
    expires_val = insert_params[3]
    assert isinstance(expires_val, datetime)
    assert expires_val.tzinfo is not None
    assert expires_val.year == 2027

    assert result["expires_at"] == "2027-01-01T00:00:00+00:00"
