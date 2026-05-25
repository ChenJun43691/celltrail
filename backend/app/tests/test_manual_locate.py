"""
PATCH /api/projects/{p}/raw-traces/{id}/manual-locate 業務邏輯測試
（2026-05-25，WAKE_UP_TODO #8）

涵蓋：
  • 權限：assert_project_access 用 'collaborator' min（admin 短路）
  • 404：trace 不存在 / 不屬於該 project / 已軟刪 —— 對外統一 404
  • 409：SELECT 後並發軟刪 → UPDATE rowcount=0
  • Pydantic 範圍驗證：lat/lng 越界（程式不會走到 endpoint）
  • SQL 順序：ST_MakePoint(lng, lat) 與直覺相反，必須是 (x=lng, y=lat)
  • UPDATE 三欄一致：lat / lng / geom 同步更新
  • Audit：詳實記錄 prev_lat/prev_lng/prev_has_geom（repin 軌跡）
  • Audit：失敗也寫 failure 版本（forensic 完整性）

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
# Fake infra（多 SQL 場景 → scripted cursor + rowcount）
# ─────────────────────────────────────────────────────────────
class _ScriptedCursor:
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
        return self._fetches.pop(0) if self._fetches else None


class _ScriptedConn:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self): return self._cursor


def _install_scripted_conn(monkeypatch, captured, fetch_results, rowcount_results=None):
    """注意：每次 with get_conn() as conn: 都會 yield 同一個 _ScriptedCursor，
    讓 captured 跨多次 get_conn() 累積。endpoint 內 SELECT 與 UPDATE 是兩次
    `with get_conn()` 區塊。"""
    cur = _ScriptedCursor(captured, fetch_results, rowcount_results or [])
    @contextmanager
    def fake_get_conn(): yield _ScriptedConn(cur)
    import app.api.map as map_mod
    monkeypatch.setattr(map_mod, "get_conn", fake_get_conn)


def _spy_write_audit(monkeypatch):
    calls: list[dict] = []
    def spy(**kwargs):
        calls.append(kwargs)
        return 999
    import app.api.map as map_mod
    monkeypatch.setattr(map_mod, "write_audit", spy)
    return calls


def _stub_perm_pass(monkeypatch):
    """assert_project_access 短路（讓被測函式專注於業務邏輯本身）。"""
    import app.api.map as map_mod
    monkeypatch.setattr(map_mod, "assert_project_access",
                        lambda user, pid, perm="viewer": None)


def _stub_perm_reject(monkeypatch, http_status: int = 403):
    import app.api.map as map_mod
    def boom(user, pid, perm="viewer"):
        raise HTTPException(status_code=http_status, detail=f"need {perm}")
    monkeypatch.setattr(map_mod, "assert_project_access", boom)


def _make_request():
    """簡化 Request（write_audit 用 client.host / headers）。"""
    class _C: host = "127.0.0.1"
    class _R:
        client = _C()
        headers = {}
    return _R()


# ─────────────────────────────────────────────────────────────
# A. 權限路徑
# ─────────────────────────────────────────────────────────────
def test_manual_locate_requires_collaborator(monkeypatch):
    """viewer → assert_project_access raise 403，UPDATE 不該走到。"""
    import app.api.map as m
    _stub_perm_reject(monkeypatch, 403)
    captured = []
    _install_scripted_conn(monkeypatch, captured, fetch_results=[])

    body = m.ManualLocateIn(lat=22.6, lng=120.3, note=None)
    with pytest.raises(HTTPException) as exc:
        m.manual_locate_trace(
            project_id="P-1", trace_id=42,
            request=_make_request(), body=body,
            current_user={"id": 5, "role": "user"},
        )
    assert exc.value.status_code == 403
    assert captured == [], "viewer 不該觸發任何 SQL"


def test_manual_locate_permission_min_is_collaborator(monkeypatch):
    """
    驗證 assert_project_access 被呼叫時 min_permission 確實是 'collaborator'。
    （守住設計決定：viewer 不能改證據資料；admin 由 assert 內部短路。）
    """
    import app.api.map as m
    calls: list = []
    def spy(user, pid, perm="viewer"):
        calls.append({"user": user, "pid": pid, "perm": perm})
    monkeypatch.setattr(m, "assert_project_access", spy)

    # SELECT → None → 404；測試只關心 assert 怎麼被呼叫
    _install_scripted_conn(monkeypatch, [], fetch_results=[None])
    _spy_write_audit(monkeypatch)

    body = m.ManualLocateIn(lat=22.6, lng=120.3)
    with pytest.raises(HTTPException):
        m.manual_locate_trace(
            project_id="P-1", trace_id=42,
            request=_make_request(), body=body,
            current_user={"id": 5, "role": "user"},
        )
    assert calls == [{"user": {"id": 5, "role": "user"}, "pid": "P-1", "perm": "collaborator"}]


# ─────────────────────────────────────────────────────────────
# B. Pydantic 範圍驗證
# ─────────────────────────────────────────────────────────────
def test_lat_out_of_range_rejected_at_model():
    """lat=91 → Pydantic validation error（在進 endpoint 前就擋下）。"""
    from app.api.map import ManualLocateIn
    with pytest.raises(Exception):  # pydantic.ValidationError
        ManualLocateIn(lat=91.0, lng=120.0)


def test_lng_out_of_range_rejected_at_model():
    from app.api.map import ManualLocateIn
    with pytest.raises(Exception):
        ManualLocateIn(lat=22.0, lng=181.0)


def test_note_too_long_rejected_at_model():
    """note 長度上限 500（避免使用者貼整本 PDF 進來）。"""
    from app.api.map import ManualLocateIn
    with pytest.raises(Exception):
        ManualLocateIn(lat=22.0, lng=120.0, note="x" * 501)


# ─────────────────────────────────────────────────────────────
# C. 404 / 409 路徑
# ─────────────────────────────────────────────────────────────
def test_trace_not_found_returns_404(monkeypatch):
    """SELECT 回 None（不存在 / 不屬於此 project / 已軟刪）→ 統一 404。"""
    import app.api.map as m
    _stub_perm_pass(monkeypatch)
    _install_scripted_conn(monkeypatch, [], fetch_results=[None])
    audit_calls = _spy_write_audit(monkeypatch)

    body = m.ManualLocateIn(lat=22.6, lng=120.3)
    with pytest.raises(HTTPException) as exc:
        m.manual_locate_trace(
            project_id="P-1", trace_id=999,
            request=_make_request(), body=body,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 404
    assert "找不到" in exc.value.detail
    # 404 不寫 audit（沒事件可記）
    assert audit_calls == []


def test_concurrent_soft_delete_returns_409(monkeypatch):
    """SELECT 拿到舊值後、UPDATE 之間並發軟刪 → rowcount=0 → 409。"""
    import app.api.map as m
    _stub_perm_pass(monkeypatch)
    # SELECT 拿到舊值 → UPDATE rowcount=0
    _install_scripted_conn(monkeypatch, [],
                           fetch_results=[(22.5, 120.1, True)],
                           rowcount_results=[0])
    _spy_write_audit(monkeypatch)

    body = m.ManualLocateIn(lat=22.6, lng=120.3)
    with pytest.raises(HTTPException) as exc:
        m.manual_locate_trace(
            project_id="P-1", trace_id=42,
            request=_make_request(), body=body,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 409


# ─────────────────────────────────────────────────────────────
# D. 成功路徑 + SQL 順序 + audit
# ─────────────────────────────────────────────────────────────
def test_success_first_pin_writes_audit_with_prev_none(monkeypatch):
    """
    首次標位置（prev_has_geom=False）→ 200 + repin=False
    + audit 含 prev_lat=None, prev_lng=None, prev_has_geom=False。
    """
    import app.api.map as m
    _stub_perm_pass(monkeypatch)
    captured = []
    # SELECT → (None, None, False)；UPDATE rowcount=1
    _install_scripted_conn(monkeypatch, captured,
                           fetch_results=[(None, None, False)],
                           rowcount_results=[1])
    audit_calls = _spy_write_audit(monkeypatch)

    body = m.ManualLocateIn(lat=22.6, lng=120.3, note="現場照片標記")
    result = m.manual_locate_trace(
        project_id="P-1", trace_id=42,
        request=_make_request(), body=body,
        current_user={"id": 1, "role": "admin"},
    )

    assert result == {
        "ok": True, "trace_id": 42, "project_id": "P-1",
        "lat": 22.6, "lng": 120.3, "repin": False,
    }

    # 兩次 SQL：SELECT + UPDATE
    assert len(captured) == 2
    assert "SELECT" in captured[0]["sql"].upper()
    assert "UPDATE" in captured[1]["sql"].upper()

    # UPDATE params：(lat, lng, lng_for_makepoint, lat_for_makepoint, id, project_id)
    # 守住 ST_MakePoint(x, y) 是 (lng, lat) 順序的契約
    up_params = captured[1]["params"]
    assert up_params == (22.6, 120.3, 120.3, 22.6, 42, "P-1")

    # audit
    assert len(audit_calls) == 1
    a = audit_calls[0]
    assert a["action"] == "manual_locate"
    assert a["target_type"] == "raw_traces"
    assert a["target_ref"] == "42"
    assert a["project_id"] == "P-1"
    assert a["status_code"] == 200
    assert a["details"] == {
        "lat": 22.6, "lng": 120.3, "note": "現場照片標記",
        "prev_lat": None, "prev_lng": None, "prev_has_geom": False,
    }


def test_success_repin_records_prev_values(monkeypatch):
    """repin（已有 geom）→ audit details.prev_has_geom=True + prev_lat/lng 為舊值。"""
    import app.api.map as m
    _stub_perm_pass(monkeypatch)
    _install_scripted_conn(monkeypatch, [],
                           fetch_results=[(22.5, 120.1, True)],
                           rowcount_results=[1])
    audit_calls = _spy_write_audit(monkeypatch)

    body = m.ManualLocateIn(lat=22.62, lng=120.31, note=None)
    result = m.manual_locate_trace(
        project_id="P-1", trace_id=42,
        request=_make_request(), body=body,
        current_user={"id": 1, "role": "admin"},
    )
    assert result["repin"] is True

    a = audit_calls[0]
    assert a["details"]["prev_lat"] == 22.5
    assert a["details"]["prev_lng"] == 120.1
    assert a["details"]["prev_has_geom"] is True


def test_db_error_writes_failure_audit(monkeypatch):
    """UPDATE 拋例外 → 寫 failure audit (action='manual_locate_failed') + 500。"""
    import app.api.map as m
    _stub_perm_pass(monkeypatch)

    # 自製 cursor：SELECT 正常、UPDATE 拋
    class _PartialCursor:
        rowcount = 0
        def __init__(self): self._calls = 0
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, sql, params=None, *, prepare=None):
            self._calls += 1
            if self._calls == 2:
                raise RuntimeError("simulated pg outage")
        def fetchone(self):
            return (None, None, False)

    cur = _PartialCursor()
    class _C:
        def cursor(self): return cur
    @contextmanager
    def fake_conn(): yield _C()
    monkeypatch.setattr(m, "get_conn", fake_conn)

    audit_calls = _spy_write_audit(monkeypatch)

    body = m.ManualLocateIn(lat=22.6, lng=120.3, note="x")
    with pytest.raises(HTTPException) as exc:
        m.manual_locate_trace(
            project_id="P-1", trace_id=42,
            request=_make_request(), body=body,
            current_user={"id": 1, "role": "admin"},
        )
    assert exc.value.status_code == 500

    assert len(audit_calls) == 1
    a = audit_calls[0]
    assert a["action"] == "manual_locate_failed"
    assert a["status_code"] == 500
    assert "simulated pg outage" in a["error_text"]
