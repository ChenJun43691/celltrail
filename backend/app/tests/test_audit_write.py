"""
write_audit() 業務邏輯測試（2026-05-24）

填補 WAKE_UP_TODO #2「業務邏輯層偏薄」的一塊：
test_audit.py 已覆蓋 _client_ip / _hash_payload / _user_agent 純函式，
但 write_audit 本身的 safe 失敗語意、user/details 欄位組裝、SQL params
對應、details 不污染呼叫端 dict 等性質完全沒守。

不依賴真實 DB —— 把 app.services.audit.get_conn 換成 FakeConn，
測試只驗 SQL params 內容與 safe 行為。
"""
from __future__ import annotations

import os
from contextlib import contextmanager

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# ─────────────────────────────────────────────────────────────
# Fake DB infra（最小可用，剛好滿足 write_audit 的 cur.execute + fetchone）
# ─────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, captured: list, fetch_result=(123,), raise_on_execute: Exception | None = None):
        self._captured = captured
        self._fetch = fetch_result
        self._raise = raise_on_execute

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None, *, prepare=None):
        if self._raise:
            raise self._raise
        # 紀錄供 assert
        self._captured.append({"sql": sql, "params": params, "prepare": prepare})

    def fetchone(self):
        return self._fetch


class _FakeConn:
    def __init__(self, captured: list, fetch_result=(123,), raise_on_execute: Exception | None = None):
        self._captured = captured
        self._fetch = fetch_result
        self._raise = raise_on_execute

    def cursor(self):
        return _FakeCursor(self._captured, self._fetch, self._raise)


def _install_fake_get_conn(monkeypatch, captured: list, fetch_result=(123,), raise_on_execute: Exception | None = None):
    @contextmanager
    def fake_get_conn():
        yield _FakeConn(captured, fetch_result, raise_on_execute)

    import app.services.audit as audit_mod
    monkeypatch.setattr(audit_mod, "get_conn", fake_get_conn)


# ─────────────────────────────────────────────────────────────
# A. 正常路徑：欄位組裝
# ─────────────────────────────────────────────────────────────
def test_write_audit_returns_new_id(monkeypatch):
    """成功寫入 → 回 RETURNING 的 id（int）"""
    captured: list = []
    _install_fake_get_conn(monkeypatch, captured, fetch_result=(999,))

    from app.services.audit import write_audit

    rid = write_audit(action="upload", user={"id": 1, "username": "alice", "role": "admin"})
    assert rid == 999
    assert len(captured) == 1, "應只執行一次 INSERT"


def test_write_audit_user_fields_propagate(monkeypatch):
    """user dict 的 id/username/role 應分別寫入對應 SQL params"""
    captured: list = []
    _install_fake_get_conn(monkeypatch, captured)

    from app.services.audit import write_audit

    write_audit(
        action="delete_target",
        user={"id": 42, "username": "bob", "role": "user"},
        target_type="target",
        target_ref="T-123",
        project_id="P-9",
        details={"reason": "duplicate"},
    )

    params = captured[0]["params"]
    # 對齊 INSERT 欄位順序：
    #   user_id, username, role,
    #   action, target_type, target_ref, project_id,
    #   ip, user_agent,
    #   details, payload_hash,
    #   status_code, error_text
    assert params[0] == 42
    assert params[1] == "bob"
    assert params[2] == "user"
    assert params[3] == "delete_target"
    assert params[4] == "target"
    assert params[5] == "T-123"
    assert params[6] == "P-9"
    # ip / ua 沒給 request → None
    assert params[7] is None
    assert params[8] is None
    # details 序列化成 JSON 字串
    assert '"reason":' in params[9] and '"duplicate"' in params[9]
    # payload_hash 應為 64 字 hex
    assert isinstance(params[10], str) and len(params[10]) == 64


def test_write_audit_no_user_all_user_fields_none(monkeypatch):
    """user=None（背景任務、匿名情境）→ user_id/username/role 都是 None，不拋例外"""
    captured: list = []
    _install_fake_get_conn(monkeypatch, captured)

    from app.services.audit import write_audit

    rid = write_audit(action="background_job", user=None)
    assert rid == 123  # FakeCursor 預設

    params = captured[0]["params"]
    assert params[0] is None
    assert params[1] is None
    assert params[2] is None


def test_write_audit_details_none_defaults_to_empty(monkeypatch):
    """details=None → 序列化為 '{}'，payload_hash 等於空 dict 的 SHA-256"""
    captured: list = []
    _install_fake_get_conn(monkeypatch, captured)

    from app.services.audit import write_audit, _hash_payload

    write_audit(action="ping", user=None, details=None)
    params = captured[0]["params"]
    assert params[9] == "{}"
    assert params[10] == _hash_payload({})


def test_write_audit_does_not_mutate_caller_details(monkeypatch):
    """
    details 進入 write_audit 後若被 in-place mutate，呼叫端再 reuse 同個
    dict 時會被污染。驗證 write_audit 至少對頂層做 copy（dict(details or {})）。
    """
    captured: list = []
    _install_fake_get_conn(monkeypatch, captured)

    from app.services.audit import write_audit

    caller_dict = {"k": "v"}
    write_audit(action="x", user=None, details=caller_dict)
    # write_audit 內未來若加 details["audited_at"] = now() 之類副作用，這條就會炸
    assert caller_dict == {"k": "v"}, "write_audit 不應污染呼叫端 details dict"


def test_write_audit_status_and_error_fields(monkeypatch):
    """status_code / error_text 走獨立欄位（不是 details 內），方便 SQL filter"""
    captured: list = []
    _install_fake_get_conn(monkeypatch, captured)

    from app.services.audit import write_audit

    write_audit(
        action="upload_failed",
        user={"id": 1, "username": "c", "role": "user"},
        status_code=500,
        error_text="DB pool exhausted",
    )
    params = captured[0]["params"]
    # 對齊欄位順序的最後兩格
    assert params[11] == 500
    assert params[12] == "DB pool exhausted"


# ─────────────────────────────────────────────────────────────
# B. safe 失敗語意（最關鍵的可靠性合約）
# ─────────────────────────────────────────────────────────────
def test_write_audit_safe_true_swallows_db_error(monkeypatch, capsys):
    """
    safe=True（預設）：DB 失敗應被吞、回 None、print [audit] WARN。
    這是「審計缺一筆比讓使用者上傳失敗更可接受」的核心合約 —— 不能
    悄悄變成 raise，否則整條上傳鏈會被 audit 拖垮。
    """
    captured: list = []
    boom = RuntimeError("simulated pg outage")
    _install_fake_get_conn(monkeypatch, captured, raise_on_execute=boom)

    from app.services.audit import write_audit

    rid = write_audit(action="upload", user={"id": 1, "username": "c", "role": "user"})
    assert rid is None

    out = capsys.readouterr().out
    assert "[audit]" in out and "WARN" in out, "失敗應 print 警告而非靜默"
    assert "upload" in out, "warn 訊息應含 action 名"


def test_write_audit_safe_false_reraises(monkeypatch):
    """safe=False：失敗應原樣拋出，供呼叫端決定處理"""
    captured: list = []
    boom = RuntimeError("simulated pg outage")
    _install_fake_get_conn(monkeypatch, captured, raise_on_execute=boom)

    from app.services.audit import write_audit
    import pytest

    with pytest.raises(RuntimeError, match="simulated pg outage"):
        write_audit(
            action="upload",
            user={"id": 1, "username": "c", "role": "user"},
            safe=False,
        )


# ─────────────────────────────────────────────────────────────
# C. payload_hash 一致性（事後驗證錨點）
# ─────────────────────────────────────────────────────────────
def test_write_audit_payload_hash_matches_independent_compute(monkeypatch):
    """SQL params 第 11 欄（payload_hash）必須等於 _hash_payload(details)。

    這是「事後比對」可不可信的根 —— 若 write_audit 內部偷偷對 details
    加料（例如塞 timestamp），事後用呼叫端原 details 重算就對不上。
    """
    captured: list = []
    _install_fake_get_conn(monkeypatch, captured)

    from app.services.audit import write_audit, _hash_payload

    details = {"filename": "x.csv", "inserted": 100, "ext": "csv"}
    write_audit(action="upload", user=None, details=details)

    params = captured[0]["params"]
    assert params[10] == _hash_payload(details), (
        "write_audit 寫入的 payload_hash 必須能由呼叫端用同樣 details 重算驗證"
    )
