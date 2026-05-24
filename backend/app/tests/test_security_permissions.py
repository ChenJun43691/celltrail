"""
assert_project_access / _PERM_LEVELS / get_current_user_optional 業務邏輯測試
（2026-05-24）

這支是整個專案層權限的安全核心。沒有專屬測試會讓未來改 permission
level 順序、SQL filter、或 AUTH_ENABLED 短路條件時靜默回歸 —— 而
這條鏈一旦壞掉就是「未授權者可看別人案件」。

不依賴 DB：monkeypatch app.security.get_conn 為 FakeConn。
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
# Fake DB infra
# ─────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, fetch_result):
        self._fetch = fetch_result

    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def execute(self, sql, params=None, *, prepare=None):
        pass  # 不驗 SQL 內容，只關心 fetch_result 與行為

    def fetchone(self):
        return self._fetch


class _FakeConn:
    def __init__(self, fetch_result):
        self._fetch = fetch_result

    def cursor(self):
        return _FakeCursor(self._fetch)


def _install_fake_get_conn(monkeypatch, fetch_result):
    """fetch_result=None → 模擬 SELECT 無此 row；(perm,) → 模擬有授權"""
    @contextmanager
    def fake_get_conn():
        yield _FakeConn(fetch_result)
    import app.security as sec
    monkeypatch.setattr(sec, "get_conn", fake_get_conn)


# ─────────────────────────────────────────────────────────────
# A. _PERM_LEVELS 層級序（如果這條被改動，後面所有比較全錯）
# ─────────────────────────────────────────────────────────────
def test_perm_levels_strict_ordering():
    """viewer(0) < collaborator(1) < owner(2)；數字不可改"""
    from app.security import _PERM_LEVELS
    assert _PERM_LEVELS["viewer"] == 0
    assert _PERM_LEVELS["collaborator"] == 1
    assert _PERM_LEVELS["owner"] == 2
    # 維持嚴格遞增（避免未來插入新 level 時誤把序打亂）
    assert _PERM_LEVELS["viewer"] < _PERM_LEVELS["collaborator"] < _PERM_LEVELS["owner"]


# ─────────────────────────────────────────────────────────────
# B. assert_project_access 短路路徑
# ─────────────────────────────────────────────────────────────
def test_assert_project_access_admin_bypasses_db(monkeypatch):
    """role=admin → 直接通過，不應碰 DB"""
    @contextmanager
    def explosive_get_conn():
        raise AssertionError("admin 不該觸發 DB 查詢")
        yield  # pragma: no cover
    import app.security as sec
    monkeypatch.setattr(sec, "get_conn", explosive_get_conn)

    from app.security import assert_project_access
    # 不該拋例外
    assert_project_access({"id": 1, "role": "admin"}, "P-1", "owner")


def test_assert_project_access_auth_disabled_bypasses_db(monkeypatch):
    """AUTH_ENABLED=false → 直接通過（anonymous admin 路徑）"""
    @contextmanager
    def explosive_get_conn():
        raise AssertionError("AUTH_ENABLED=false 不該觸發 DB 查詢")
        yield  # pragma: no cover
    import app.security as sec
    monkeypatch.setattr(sec, "get_conn", explosive_get_conn)
    monkeypatch.setattr(sec, "AUTH_ENABLED", False)

    from app.security import assert_project_access
    # role 故意不是 admin，純靠 AUTH_ENABLED=false 短路
    assert_project_access({"id": 5, "role": "user"}, "P-1", "owner")


# ─────────────────────────────────────────────────────────────
# C. assert_project_access 拒絕路徑
# ─────────────────────────────────────────────────────────────
def test_assert_project_access_no_membership_rejects(monkeypatch):
    """非 admin + 該專案無成員資格 → 403「無此案件的存取權限」"""
    _install_fake_get_conn(monkeypatch, fetch_result=None)

    from app.security import assert_project_access
    with pytest.raises(HTTPException) as exc:
        assert_project_access({"id": 5, "role": "user"}, "P-1", "viewer")
    assert exc.value.status_code == 403
    assert "存取" in exc.value.detail


def test_assert_project_access_viewer_cannot_act_as_owner(monkeypatch):
    """viewer 想做 owner-only 操作 → 403，訊息含目前 permission"""
    _install_fake_get_conn(monkeypatch, fetch_result=("viewer",))

    from app.security import assert_project_access
    with pytest.raises(HTTPException) as exc:
        assert_project_access({"id": 5, "role": "user"}, "P-1", "owner")
    assert exc.value.status_code == 403
    assert "owner" in exc.value.detail
    assert "viewer" in exc.value.detail, "錯誤訊息應提示目前實際權限以利除錯"


def test_assert_project_access_collaborator_cannot_act_as_owner(monkeypatch):
    """collaborator 想做 owner-only → 403（介於兩 level 之間的關鍵邊界）"""
    _install_fake_get_conn(monkeypatch, fetch_result=("collaborator",))

    from app.security import assert_project_access
    with pytest.raises(HTTPException) as exc:
        assert_project_access({"id": 5, "role": "user"}, "P-1", "owner")
    assert exc.value.status_code == 403


def test_assert_project_access_unknown_permission_treated_as_lowest(monkeypatch):
    """
    DB 回了未知 permission 字串（資料污染情境）→ -1，永遠低於任何
    min_permission → 403。是「未知就拒絕」的保守 fallback。
    """
    _install_fake_get_conn(monkeypatch, fetch_result=("god_mode",))

    from app.security import assert_project_access
    with pytest.raises(HTTPException) as exc:
        assert_project_access({"id": 5, "role": "user"}, "P-1", "viewer")
    assert exc.value.status_code == 403


# ─────────────────────────────────────────────────────────────
# D. assert_project_access 通過路徑
# ─────────────────────────────────────────────────────────────
def test_assert_project_access_owner_passes_owner_check(monkeypatch):
    """owner 可做 owner-only 操作"""
    _install_fake_get_conn(monkeypatch, fetch_result=("owner",))
    from app.security import assert_project_access
    assert_project_access({"id": 5, "role": "user"}, "P-1", "owner")  # no raise


def test_assert_project_access_owner_passes_lower_checks(monkeypatch):
    """owner 也應能做 viewer / collaborator 等 lower 操作"""
    _install_fake_get_conn(monkeypatch, fetch_result=("owner",))
    from app.security import assert_project_access
    assert_project_access({"id": 5, "role": "user"}, "P-1", "viewer")
    assert_project_access({"id": 5, "role": "user"}, "P-1", "collaborator")


def test_assert_project_access_collaborator_passes_viewer(monkeypatch):
    """collaborator 應能做 viewer 操作（典型「列出案件成員」場景）"""
    _install_fake_get_conn(monkeypatch, fetch_result=("collaborator",))
    from app.security import assert_project_access
    assert_project_access({"id": 5, "role": "user"}, "P-1", "viewer")


def test_assert_project_access_unknown_min_permission_defaults_to_zero(monkeypatch):
    """
    min_permission 傳了未知字串 → required=0（viewer 等級）。
    這是「呼叫端拼錯字也別意外把守備降到 0」的相反保守選擇 —— 目前
    程式碼選的就是降到 0（最寬鬆）。本測試把現況釘住，未來若改為
    「拼錯就 raise」會在此提醒。
    """
    _install_fake_get_conn(monkeypatch, fetch_result=("viewer",))
    from app.security import assert_project_access
    # min_permission='typo' → 視為 0；viewer(0) >= 0 → 通過
    assert_project_access({"id": 5, "role": "user"}, "P-1", "typo")


# ─────────────────────────────────────────────────────────────
# E. get_current_user_optional 無 token / 無效 token 行為
# ─────────────────────────────────────────────────────────────
def test_get_current_user_optional_no_token_returns_none():
    from app.security import get_current_user_optional
    assert get_current_user_optional(token=None) is None


def test_get_current_user_optional_garbage_token_returns_none():
    """非 JWT 字串 → JWTError → None（不拋 401）"""
    from app.security import get_current_user_optional
    assert get_current_user_optional(token="not-a-jwt") is None


def test_get_current_user_optional_valid_token_returns_user(monkeypatch):
    """合法 JWT + DB 有 user → 回 user dict"""
    from app.security import create_access_token
    import app.security as sec

    fake_user = {
        "id": 7, "username": "alice", "role": "user",
        "is_active": True, "must_change_password": False,
        "real_name": None, "unit": None, "badge_number": None, "email": None,
    }
    monkeypatch.setattr(sec, "get_user_by_username", lambda u: fake_user if u == "alice" else None)

    token = create_access_token({"sub": "alice"})
    from app.security import get_current_user_optional
    u = get_current_user_optional(token=token)
    assert u is not None
    assert u["username"] == "alice"
    assert u["id"] == 7


def test_get_current_user_optional_inactive_user_returns_none(monkeypatch):
    """合法 JWT + DB user 但 is_active=False → None（不漏給已停用帳號）"""
    from app.security import create_access_token
    import app.security as sec

    inactive = {
        "id": 7, "username": "fired", "role": "user", "is_active": False,
        "must_change_password": False, "real_name": None, "unit": None,
        "badge_number": None, "email": None,
    }
    monkeypatch.setattr(sec, "get_user_by_username", lambda u: inactive)

    token = create_access_token({"sub": "fired"})
    from app.security import get_current_user_optional
    assert get_current_user_optional(token=token) is None


def test_get_current_user_optional_token_without_sub_returns_none(monkeypatch):
    """JWT 合法但 payload 沒 sub 欄位 → None"""
    from app.security import create_access_token
    from app.security import get_current_user_optional

    token = create_access_token({"not_sub": "alice"})
    assert get_current_user_optional(token=token) is None


# ─────────────────────────────────────────────────────────────
# F. anonymous admin 範本不被外部 mutate 污染（regression guard）
# ─────────────────────────────────────────────────────────────
def test_anonymous_admin_template_isolated_across_calls(monkeypatch):
    """
    AUTH_ENABLED=false 時兩次取得的 anonymous admin dict 必須是獨立副本；
    呼叫端若 mutate 自己那份不能污染下次呼叫（_ANONYMOUS_ADMIN 共用範本）。
    """
    import app.security as sec
    monkeypatch.setattr(sec, "AUTH_ENABLED", False)
    from app.security import get_current_user, get_current_user_optional

    u1 = get_current_user(token=None)
    u1["role"] = "user"   # 模擬呼叫端意外覆寫
    u1["evil"] = "polluted"

    u2 = get_current_user(token=None)
    assert u2["role"] == "admin", "_ANONYMOUS_ADMIN 範本不該被前次呼叫污染"
    assert "evil" not in u2

    u3 = get_current_user_optional(token=None)
    assert u3["role"] == "admin"
    assert "evil" not in u3
