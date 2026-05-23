# backend/app/tests/test_api_p3p7.py
"""
P3–P7 API 契約與 auth 守衛測試（DB-free，CI 可直接執行）。

背景：
- P3–P7 的 API（auth / users / account-requests / members / share /
  parse-only / format-reports / cell-towers / carrier-profile）原本只有
  手動驗證、無自動化測試 —— 這是專案完成度評估點名的最大缺口。
- 本檔沿用 test_smoke.py 的手法：monkeypatch DB 連線池，使 app 能啟動
  但不真的連 DB，因此 CI 無 DB 也能跑。
- 涵蓋兩件事：
  ① 路由契約：所有 P3–P7 端點以正確 path + method 註冊（OpenAPI）。
  ② auth 守衛：受保護端點未帶／帶壞 token 必須回 401。
     這攔得住最危險的回歸 —— 某端點不小心掉了驗證依賴而對外裸奔。
- DB 行為層（分享連結 30 分鐘效期、410 Gone、權限分級…）需另以整合
  測試覆蓋，不在本檔範圍。

設計取捨：
- auth 守衛測試只挑「無 request body」的端點（GET / DELETE / 無 body 的
  POST·PATCH）。原因：帶 body 的端點在「未帶 token」時，body 驗證(422)
  與 auth 檢查(401)的回應順序在 FastAPI 不保證，斷言會脆弱。所有守衛都
  共用 get_current_user / require_admin，挑無 body 端點即足以驗證守衛機制。
"""
import os

import pytest

# ── 必須在 import app 之前設好環境變數（與 test_smoke.py 同手法）──
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5500")
os.environ.setdefault("AUTH_ENABLED", "true")  # 測試固定走 JWT 驗證路徑


@pytest.fixture()
def client(monkeypatch):
    """TestClient：把 DB 連線池架空，app 可啟動但不真的連 DB。"""
    from app.db import session as db_session

    monkeypatch.setattr(db_session.pool, "open", lambda: None)
    monkeypatch.setattr(db_session.pool, "wait", lambda timeout=0: None)
    monkeypatch.setattr(db_session.pool, "close", lambda: None)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


# ============================================================
# ① 路由契約：所有 P3–P7 端點都應註冊
# ============================================================
# (method, OpenAPI path) —— path 參數名須與實際註冊一致
EXPECTED_ROUTES = [
    # auth
    ("post",   "/api/auth/login"),
    ("get",    "/api/auth/me"),
    ("post",   "/api/auth/change-password"),
    # users
    ("get",    "/api/users"),
    ("post",   "/api/users"),
    ("get",    "/api/users/search"),
    ("patch",  "/api/users/{user_id}"),
    ("patch",  "/api/users/{user_id}/deactivate"),
    ("patch",  "/api/users/{user_id}/reactivate"),
    # account-requests
    ("post",   "/api/account-requests"),
    ("get",    "/api/account-requests"),
    ("post",   "/api/account-requests/{request_id}/approve"),
    ("post",   "/api/account-requests/{request_id}/reject"),
    # members
    ("get",    "/api/projects/"),
    ("get",    "/api/projects/{project_id}/members"),
    ("post",   "/api/projects/{project_id}/members"),
    ("delete", "/api/projects/{project_id}/members/{user_id}"),
    # 上傳定位透明化（2026-05-23）：coverage 聚合 + unlocated 列表 + CSV
    ("get",    "/api/projects/{project_id}/coverage"),
    ("get",    "/api/projects/{project_id}/unlocated"),
    ("get",    "/api/projects/{project_id}/unlocated.csv"),
    # share（P7）—— 注意 share-links 為複數
    ("post",   "/api/projects/{project_id}/share-links"),
    ("get",    "/api/projects/{project_id}/share-links"),
    ("delete", "/api/share-links/{token}"),
    ("get",    "/api/share/{token}"),
    # parse-only / format-reports
    ("post",   "/api/parse-only"),
    ("post",   "/api/format-reports"),
    ("get",    "/api/format-reports"),
    ("patch",  "/api/format-reports/{report_id}"),
    # cell-towers / carrier-profile（admin）
    ("get",    "/api/admin/cell-towers/stats"),
    ("post",   "/api/admin/cell-towers/import"),
    ("delete", "/api/admin/cell-towers"),
    ("get",    "/api/admin/carrier-profile"),
    ("patch",  "/api/admin/carrier-profile/entry"),
    ("delete", "/api/admin/carrier-profile/entry"),
]


def test_p3p7_routes_registered(client):
    """所有 P3–P7 端點都應以正確 path + method 出現在 OpenAPI。"""
    paths = client.get("/api/openapi.json").json()["paths"]
    missing = [
        f"{method.upper()} {path}"
        for method, path in EXPECTED_ROUTES
        if path not in paths or method not in paths[path]
    ]
    assert not missing, f"OpenAPI 缺少預期端點：{missing}"


# ============================================================
# ② auth 守衛：受保護端點未帶 / 帶壞 token 必須 401
# ============================================================
# 只列「無 request body」的端點（見檔首設計取捨）。
PROTECTED_NO_BODY = [
    ("get",    "/api/auth/me"),
    ("get",    "/api/users"),
    ("get",    "/api/account-requests"),
    ("get",    "/api/projects/"),
    ("get",    "/api/projects/demo/members"),
    ("get",    "/api/projects/demo/share-links"),
    ("post",   "/api/projects/demo/share-links"),   # create_share_link 無 body
    ("delete", "/api/share-links/sometoken"),
    ("delete", "/api/projects/demo/members/1"),
    # 上傳定位透明化（2026-05-23）
    ("get",    "/api/projects/demo/coverage"),
    ("get",    "/api/projects/demo/unlocated"),
    ("get",    "/api/projects/demo/unlocated.csv"),
    ("patch",  "/api/users/1/deactivate"),          # 無 body
    ("patch",  "/api/users/1/reactivate"),          # 無 body
    ("get",    "/api/format-reports"),
    ("get",    "/api/admin/cell-towers/stats"),
    ("delete", "/api/admin/cell-towers"),
    ("get",    "/api/admin/carrier-profile"),
]


@pytest.mark.parametrize("method,path", PROTECTED_NO_BODY)
def test_protected_rejects_no_token(client, method, path):
    """受保護端點未帶 token → 必須 401（守衛在 DB 查詢之前生效）。"""
    r = getattr(client, method)(path)
    assert r.status_code == 401, (
        f"{method.upper()} {path} 未帶 token 應回 401，實得 {r.status_code}"
        " —— 該端點可能掉了 auth 依賴"
    )


@pytest.mark.parametrize("method,path", PROTECTED_NO_BODY)
def test_protected_rejects_bad_token(client, method, path):
    """受保護端點帶無效 token → 必須 401。"""
    r = getattr(client, method)(
        path, headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert r.status_code == 401, (
        f"{method.upper()} {path} 帶壞 token 應回 401，實得 {r.status_code}"
    )


# ============================================================
# ③ 公開端點：不需 token，但仍會做 body 驗證（不應回 401）
# ============================================================
def test_login_is_public(client):
    """POST /auth/login 不需 token；缺帳密 → 422（表單驗證），不是 401。"""
    r = client.post("/api/auth/login")
    assert r.status_code == 422


def test_parse_only_is_public(client):
    """POST /parse-only 不需 token（訪客免登入預覽）；缺檔案 → 422，不是 401。"""
    r = client.post("/api/parse-only")
    assert r.status_code == 422
