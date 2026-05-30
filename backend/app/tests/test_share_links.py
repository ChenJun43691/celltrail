"""
P7 分享連結：owner 守衛 + 公開檢視狀態機測試（DB-free，CI 可直接執行）。

背景：
- test_api_p3p7.py 檔首第 15-16 行明列「分享連結 30 分鐘效期、410 Gone、
  權限分級…需另以整合測試覆蓋，不在本檔範圍」—— 本檔補上那個缺口。
- 補的是兩塊現有測試都沒碰到的行為：
  ① share.py 的 `_require_project_owner` 是「只認 owner」的獨立守衛，比
     security.assert_project_access（viewer<collaborator<owner 分級）更嚴格
     —— 連 collaborator 都要擋。test_security_permissions.py 測的是
     assert_project_access，測不到這支獨立函式。
  ② GET /api/share/{token} 的四態狀態機：不存在 404 / 已撤銷 410 /
     已過期 410 / 有效 200。先前只有手動 curl 驗證、未自動化。
- 維持專案慣例（見 test_smoke.py / test_api_p3p7.py）：monkeypatch DB 連線，
  CI 無 DB / Redis / Google 也能跑。

對應手動邊界測試（2026-05-30 session）：
  viewer 擋寫入 / 非成員存取被拒 → assert_project_access 已由
  test_security_permissions.py 單元覆蓋；本檔專注 share 專屬守衛與狀態機。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

# ── 必須在 import app 之前設好環境變數（與 test_api_p3p7.py 同手法）──
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5500")
os.environ.setdefault("AUTH_ENABLED", "true")  # 固定走 JWT 驗證路徑


# ─────────────────────────────────────────────────────────────
# Fake DB infra（同 test_security_permissions.py）
# ─────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, fetch_result):
        self._fetch = fetch_result

    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def execute(self, sql, params=None, *, prepare=None):
        pass  # 不驗 SQL 內容，只關心 fetch_result 與分支行為

    def fetchone(self):
        return self._fetch


class _FakeConn:
    def __init__(self, fetch_result):
        self._fetch = fetch_result

    def cursor(self):
        return _FakeCursor(self._fetch)


def _install_fake_get_conn(monkeypatch, fetch_result):
    """把 app.api.share.get_conn 換成回傳 fetch_result 的假連線。"""
    @contextmanager
    def fake_get_conn():
        yield _FakeConn(fetch_result)
    import app.api.share as share
    monkeypatch.setattr(share, "get_conn", fake_get_conn)


# ═════════════════════════════════════════════════════════════
# A. _require_project_owner —— 分享連結的「只認 owner」守衛
#    比 assert_project_access 嚴：owner 之外（含 collaborator）全擋。
# ═════════════════════════════════════════════════════════════
def test_require_owner_admin_bypasses_db(monkeypatch):
    """role=admin → 直接通過，不應碰 DB。"""
    @contextmanager
    def explosive():
        raise AssertionError("admin 不該觸發 DB 查詢")
        yield  # pragma: no cover
    import app.api.share as share
    monkeypatch.setattr(share, "get_conn", explosive)
    share._require_project_owner("P-1", {"id": 1, "role": "admin"})  # 不該 raise


def test_require_owner_owner_passes(monkeypatch):
    """專案 owner → 通過。"""
    _install_fake_get_conn(monkeypatch, ("owner",))
    import app.api.share as share
    share._require_project_owner("P-1", {"id": 5, "role": "user"})  # 不該 raise


def test_require_owner_collaborator_rejected(monkeypatch):
    """
    關鍵邊界：collaborator 在 assert_project_access(collaborator) 會過，
    但「建立／撤銷分享連結」要求 owner —— 必須 403。這正是 share 守衛
    與通用守衛分歧、且最容易在重構時被誤放寬的地方。
    """
    _install_fake_get_conn(monkeypatch, ("collaborator",))
    import app.api.share as share
    with pytest.raises(HTTPException) as exc:
        share._require_project_owner("P-1", {"id": 5, "role": "user"})
    assert exc.value.status_code == 403
    assert "owner" in exc.value.detail


def test_require_owner_viewer_rejected(monkeypatch):
    """viewer → 403。"""
    _install_fake_get_conn(monkeypatch, ("viewer",))
    import app.api.share as share
    with pytest.raises(HTTPException) as exc:
        share._require_project_owner("P-1", {"id": 5, "role": "user"})
    assert exc.value.status_code == 403


def test_require_owner_non_member_rejected(monkeypatch):
    """非成員（DB 查無 row）→ 403。"""
    _install_fake_get_conn(monkeypatch, None)
    import app.api.share as share
    with pytest.raises(HTTPException) as exc:
        share._require_project_owner("P-1", {"id": 5, "role": "user"})
    assert exc.value.status_code == 403


# ═════════════════════════════════════════════════════════════
# B. GET /api/share/{token} —— 公開檢視四態狀態機
#    （公開端點，無 auth dependency；以 TestClient 走完整 HTTP 路徑）
# ═════════════════════════════════════════════════════════════
@pytest.fixture()
def client(monkeypatch):
    """TestClient：架空 DB 連線池，app 可啟動但不真的連 DB。"""
    from app.db import session as db_session

    monkeypatch.setattr(db_session.pool, "open", lambda: None)
    monkeypatch.setattr(db_session.pool, "wait", lambda timeout=0: None)
    monkeypatch.setattr(db_session.pool, "close", lambda: None)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _fake_share_lookup(monkeypatch, row):
    """
    讓 share_links 主查詢回 `row`，型別為 (project_id, expires_at, revoked_at)
    或 None。注意 expires_at 必須是 tz-aware datetime（handler 用
    datetime.now(timezone.utc) 比較）。
    """
    @contextmanager
    def fake_get_conn():
        yield _FakeConn(row)
    import app.api.share as share
    monkeypatch.setattr(share, "get_conn", fake_get_conn)


def test_share_view_nonexistent_returns_404(client, monkeypatch):
    """token 不存在 → 404。"""
    _fake_share_lookup(monkeypatch, None)
    r = client.get("/api/share/whatever-token")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_share_view_revoked_returns_410(client, monkeypatch):
    """已撤銷（revoked_at 有值）→ 410 Gone，即使尚未過期。"""
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    revoked = datetime.now(timezone.utc) - timedelta(minutes=1)
    _fake_share_lookup(monkeypatch, ("P-1", future, revoked))
    r = client.get("/api/share/tok")
    assert r.status_code == 410
    assert "撤銷" in r.json()["detail"]


def test_share_view_expired_returns_410(client, monkeypatch):
    """已過期（expires_at 在過去、未撤銷）→ 410 Gone。"""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    _fake_share_lookup(monkeypatch, ("P-1", past, None))
    r = client.get("/api/share/tok")
    assert r.status_code == 410
    assert "過期" in r.json()["detail"]


def test_share_view_valid_returns_200_with_geojson(client, monkeypatch):
    """
    有效（未撤銷、未過期）→ 200，body 含 geojson。
    _fetch_map_geojson 與 write_audit 都 monkeypatch 掉，使本測試只驗
    狀態機分支與回應組裝，不牽動真實 DB 查詢 / 稽核寫入。
    """
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    _fake_share_lookup(monkeypatch, ("P-1", future, None))
    import app.api.share as share
    monkeypatch.setattr(
        share, "_fetch_map_geojson",
        lambda project_id, *a, **k: {"type": "FeatureCollection", "features": [{"id": 1}]},
    )
    monkeypatch.setattr(share, "write_audit", lambda *a, **k: None)

    r = client.get("/api/share/tok")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "P-1"
    assert body["geojson"]["features"] == [{"id": 1}]
    # 純檢視端點不得洩漏任何寫入 / 報告下載入口（回應只有這三個 key）
    assert set(body.keys()) == {"project_id", "expires_at", "geojson"}
