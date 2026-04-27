# backend/app/tests/test_smoke.py
"""
Smoke tests：不依賴外部服務（DB、Redis、Google API）即可執行。

執行：
    cd backend
    source .venv/bin/activate
    pip install pytest httpx
    pytest app/tests/test_smoke.py -v

設計原則：
- 用 monkeypatch 注入假 DATABASE_URL / REDIS_URL，避免在 import 期間崩潰
- 用 TestClient 測 HTTP 行為，不實際連線 DB
- 單元測試純函式（密碼 hash、JWT、欄位標準化）
"""
import os
from datetime import timedelta

import pytest


# ============================================================
# 在 import app 之前先把必要的環境變數塞好
# ============================================================
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5500")
# 測試永遠走 JWT 驗證路徑；避免本機 .env 的 AUTH_ENABLED=false 干擾測試斷言
os.environ.setdefault("AUTH_ENABLED", "true")


# ============================================================
# 純函式測試：security
# ============================================================
def test_password_hash_and_verify():
    from app.security import hash_password, verify_password

    pwd = "p@ssw0rd!"
    hashed = hash_password(pwd)
    assert hashed != pwd, "hash 結果不應等於原密碼"
    assert verify_password(pwd, hashed) is True
    assert verify_password("wrong", hashed) is False


def test_create_access_token_decodable():
    """確認 token 可被 jose 解碼，且 sub 與 exp claim 正確。"""
    from jose import jwt

    from app.security import ALGORITHM, SECRET_KEY, create_access_token

    token = create_access_token({"sub": "alice"}, expires_delta=timedelta(minutes=5))
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

    assert payload["sub"] == "alice"
    assert "exp" in payload


# ============================================================
# 純函式測試：ingest 的欄位標準化
# ============================================================
def test_canon_and_header_mapping():
    from app.services.ingest import HEADER_MAP, _canon

    # 臺/台、全形空白、標點都要被吃掉
    assert _canon("基 地 臺  地址") == "基地台地址"

    # 確認幾個關鍵欄位能對應正確
    assert HEADER_MAP[_canon("開始連線時間")] == "start_ts"
    assert HEADER_MAP[_canon("基地台編號")] == "cell_id"
    assert HEADER_MAP[_canon("基地臺地址")] == "cell_addr"
    assert HEADER_MAP[_canon("方位角")] == "azimuth"


def test_parse_ts_various_formats():
    from app.services.ingest import _parse_ts

    assert _parse_ts("2025/08/30 13:31:22") is not None
    assert _parse_ts("2025/8/30 13:31") is not None
    assert _parse_ts("#N/A") is None
    assert _parse_ts("") is None
    assert _parse_ts(None) is None


def test_guess_accuracy():
    from app.services.ingest import _guess_accuracy

    # 市區、鄉村、其他
    assert _guess_accuracy("台北市中正區") == 150
    assert _guess_accuracy("南投縣仁愛鄉") == 800
    assert _guess_accuracy(None) == 300


# ============================================================
# HTTP 層測試（不打 DB）：驗證路由註冊 / 權限
# ============================================================
@pytest.fixture()
def client(monkeypatch):
    """
    建立 TestClient：把 DB 連線池的 open()/wait() 與 get_conn() patch 掉，
    讓 app 可以正常啟動但不真的連 DB。
    """
    # 先 patch pool 的 open/wait，使 lifespan startup 不會等太久或崩潰
    from app.db import session as db_session

    monkeypatch.setattr(db_session.pool, "open", lambda: None)
    monkeypatch.setattr(db_session.pool, "wait", lambda timeout=0: None)
    monkeypatch.setattr(db_session.pool, "close", lambda: None)
    # 註：psycopg-pool 3.2.x 沒有 wait_close() 方法，也就不需要 patch

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_root_endpoint(client):
    r = client.get("/api")
    assert r.status_code == 200
    assert r.json()["app"] == "CellTrail"


def test_openapi_schema_available(client):
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    paths = r.json().get("paths", {})
    # 核心端點都應註冊
    assert "/api" in paths
    assert "/api/auth/login" in paths
    assert "/api/auth/me" in paths
    assert "/api/upload" in paths or "/api/upload/" in paths


def test_me_requires_auth(client):
    """未帶 token 呼叫 /api/auth/me 應回 401。"""
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_map_layers_requires_auth(client):
    """地圖圖層端點需登入。"""
    r = client.get("/api/projects/demo/map-layers")
    assert r.status_code == 401


def test_delete_target_requires_auth(client):
    r = client.delete("/api/projects/demo/targets/t1")
    assert r.status_code == 401
