# backend/app/tests/test_audit.py
"""
Audit service 單元測試 —— 不依賴 DB 即可執行。

純函式覆蓋：
  - _client_ip：XFF / X-Real-IP / request.client 三段優先級
  - _hash_payload：相同 dict 不同 key 順序應產生相同 hash（canonical-json）
  - _json_default：datetime / set 都應可序列化
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

# 在 import app.services.audit 之前注入必要環境變數，避免 db.session import 時崩潰
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")  # 測試永遠走 JWT 路徑


# ---------------------------------------------------------------
# _client_ip：模擬不同 header 組合
# ---------------------------------------------------------------
class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    def __init__(self, headers: dict | None = None, client_host: str | None = None):
        # FastAPI Request.headers 是 case-insensitive，這裡用 lower-case dict 模擬即可
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = _FakeClient(client_host) if client_host else None


def test_client_ip_priority_xff():
    from app.services.audit import _client_ip

    # XFF 優先（多 IP 取第一個）
    req = _FakeRequest(
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.1, 10.0.0.2",
                "X-Real-IP": "198.51.100.5"},
        client_host="127.0.0.1",
    )
    assert _client_ip(req) == "203.0.113.10"


def test_client_ip_fallback_realip():
    from app.services.audit import _client_ip

    req = _FakeRequest(
        headers={"X-Real-IP": "198.51.100.5"},
        client_host="127.0.0.1",
    )
    assert _client_ip(req) == "198.51.100.5"


def test_client_ip_fallback_request_client():
    from app.services.audit import _client_ip

    req = _FakeRequest(headers={}, client_host="127.0.0.1")
    assert _client_ip(req) == "127.0.0.1"


def test_client_ip_none_when_request_is_none():
    from app.services.audit import _client_ip

    assert _client_ip(None) is None


def test_user_agent_extraction():
    from app.services.audit import _user_agent

    req = _FakeRequest(headers={"User-Agent": "curl/8.4.0"})
    assert _user_agent(req) == "curl/8.4.0"
    assert _user_agent(None) is None


# ---------------------------------------------------------------
# _hash_payload：canonical-json，順序不影響 hash
# ---------------------------------------------------------------
def test_hash_payload_canonical_order():
    from app.services.audit import _hash_payload

    # 不同 key 順序但內容相同 → 同 hash
    a = {"filename": "x.csv", "inserted": 100, "ext": "csv"}
    b = {"ext": "csv", "inserted": 100, "filename": "x.csv"}
    assert _hash_payload(a) == _hash_payload(b)

    # 內容不同 → 不同 hash
    c = {"filename": "x.csv", "inserted": 99, "ext": "csv"}
    assert _hash_payload(a) != _hash_payload(c)


def test_hash_payload_handles_datetime_and_set():
    from app.services.audit import _hash_payload, _json_default

    dt = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
    payload = {"ts": dt, "tags": {"a", "b"}}
    # 應不丟例外（_json_default 處理 datetime / set）
    h = _hash_payload(payload)
    assert isinstance(h, str)
    assert len(h) == 64  # SHA-256 hex length

    # 直接驗 _json_default
    assert _json_default(dt).startswith("2026-04-26")
    assert sorted(_json_default({"x", "y"})) == ["x", "y"]


def test_hash_payload_empty_dict_is_stable():
    from app.services.audit import _hash_payload

    # 同樣的空 dict 產出固定 hash（驗證確實有走 SHA-256，不是隨機）
    h1 = _hash_payload({})
    h2 = _hash_payload({})
    assert h1 == h2
    # SHA-256 of "{}" == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert h1 == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
