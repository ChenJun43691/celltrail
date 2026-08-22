# backend/app/tests/test_api_preview_guest_mapping.py
"""
Preview API — guest preview + mapping-aware preview（P9 Phase 2B）。

守住兩件在此之前被 A.3 明文排除、而本輪解除的能力，以及它們各自的安全邊界：

  1. **guest preview**：免登入建立；`created_by IS NULL` 的 artifact 以 `preview_id`
     本身作為 capability（同 P7 share_links）。要守的是「訪客能讀自己的、
     但登入者的 artifact 不會因此被別人讀走」這條界線沒有被拆掉。
  2. **mapping-aware preview**：mapping 存進 provenance，**read / save 一律以
     server 端那份重解析**。要守的是「呼叫端事後換不掉 mapping」——否則封存過的
     parsed_records_hash 會與實際落地內容脫鉤，證據鏈失去意義。

沿用 test_api_preview 的 TestClient + dependency_overrides + monkeypatch 模式，不碰真 DB。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
import app.services.preview_artifact as pa
from app.security import get_current_user, get_current_user_optional

app = main_mod.app
client = TestClient(app)

ADMIN = {"id": 1, "username": "admin", "role": "admin"}
USER = {"id": 5, "username": "u5", "role": "user"}
OTHER = {"id": 6, "username": "u6", "role": "user"}

_RECS = [{"target_id": "t", "start_ts": "2026-01-01T00:00:00", "lat": 22.6, "lng": 120.3}]


def _auth(user):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_optional] = lambda: user


def _guest():
    """訪客：optional 回 None；required 這支在真實環境會 401，這裡照樣模擬。"""
    from fastapi import HTTPException

    def _raise():
        raise HTTPException(status_code=401, detail="未登入")

    app.dependency_overrides[get_current_user] = _raise
    app.dependency_overrides[get_current_user_optional] = lambda: None


def _meta(**over):
    base = {
        "id": 10, "preview_id": "tok", "filename": "x.xlsx", "ext": "xlsx",
        "sha256_full": "sha", "row_count": 1, "parser_type": "auto",
        "provenance": {"pipeline_version": "P9", "target_id": "t"}, "created_by": 5,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "sealed_at": None,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.setattr("app.api.preview.write_audit", lambda **kw: 1)
    # 每個 test 用獨立配額 key 空間：FixedWindow 是共用 in-memory storage，
    # 不重設會讓測試之間互相污染（前一個 test 用掉的 hit 算到後一個頭上）。
    from app.services.limiter import limiter
    try:
        limiter.limiter.storage.reset()
    except Exception:
        pass
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def created(monkeypatch):
    """攔截 pa.create，回傳被送進去的 kwargs（用來驗 provenance / created_by）。"""
    box = {}

    def _create(**k):
        box.update(k)
        return {
            "preview_id": "tok_abc", "sha256_full": "sha", "size_bytes": 3,
            "storage_kind": "db", "row_count": 1,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        }

    monkeypatch.setattr(pa, "create", _create)
    return box


@pytest.fixture
def parsed(monkeypatch):
    """攔截 parse_file_only，記錄每次被呼叫時拿到的 mapping。"""
    seen = []

    def _parse(target_id, filename, content, mapping=None):
        seen.append(mapping)
        return list(_RECS)

    monkeypatch.setattr("app.api.preview.parse_file_only", _parse)
    return seen


def _post(**data):
    return client.post(
        "/api/preview",
        files={"file": ("x.xlsx", b"abc", "application/octet-stream")},
        data=data or {"target_id": "t"},
    )


# ── guest create ────────────────────────────────────────────
def test_guest_can_create_preview(created, parsed):
    _guest()
    r = _post(target_id="t")
    assert r.status_code == 200, r.text
    assert r.json()["preview_id"] == "tok_abc"
    # 訪客不可被寫進 created_by（FK 指向 users，且沒有可歸屬的身分）
    assert created["created_by"] is None
    assert created["provenance"]["origin"] == "guest"


def test_logged_in_create_records_creator_and_no_guest_origin(created, parsed):
    _auth(USER)
    r = _post(target_id="t")
    assert r.status_code == 200, r.text
    assert created["created_by"] == 5
    assert "origin" not in created["provenance"]


def test_guest_quota_returns_429_with_code(created, parsed, monkeypatch):
    """超額時回 RATE_LIMITED，且**不進解析**（省掉大檔白解析）。"""
    _guest()
    monkeypatch.setattr("app.api.preview.hit_guest_preview_quota", lambda req: False)
    r = _post(target_id="t")
    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "RATE_LIMITED"
    assert parsed == []          # 配額在讀檔/解析之前就擋下


def test_logged_in_user_is_not_rate_limited(created, parsed, monkeypatch):
    """登入者不套訪客配額 —— 一個案件動輒十幾個歷程檔，套了會誤傷辦案。"""
    _auth(USER)
    monkeypatch.setattr("app.api.preview.hit_guest_preview_quota",
                        lambda req: pytest.fail("登入者不應扣訪客配額"))
    assert _post(target_id="t").status_code == 200


def test_guest_quota_helper_actually_counts():
    """配額 helper 自身：同一 IP 連續 hit 會在 20 次後回 False。"""
    from app.services.limiter import hit_guest_preview_quota

    class _Req:
        client = type("C", (), {"host": "203.0.113.99"})()
        headers = {}
        scope = {"client": ("203.0.113.99", 1)}

    req = _Req()
    results = [hit_guest_preview_quota(req) for _ in range(22)]
    assert all(results[:20]), "前 20 次應放行"
    assert not results[20] and not results[21], "第 21 次起應擋下"


# ── guest artifact 的能力模型 ───────────────────────────────
def test_guest_artifact_readable_by_anyone_holding_the_id(monkeypatch, parsed):
    """created_by IS NULL → preview_id 即 capability（否則訪客讀不回自己剛建的預覽）。"""
    _guest()
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=None))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"abc")
    r = client.get("/api/preview/tok")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


def test_guest_artifact_claimable_after_login(monkeypatch, parsed):
    """訪客預覽 → 登入 → 同一個 preview_id 直接儲存，不需把記錄留在前端。"""
    _auth(USER)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=None))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"abc")
    assert client.get("/api/preview/tok").status_code == 200


def test_user_artifact_still_private_from_others(monkeypatch, parsed):
    """能力模型只放寬 created_by IS NULL 那一支；具名 artifact 的界線不得被拆掉。"""
    _auth(OTHER)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=5))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"abc")
    r = client.get("/api/preview/tok")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PREVIEW_FORBIDDEN"


def test_user_artifact_not_readable_by_guest(monkeypatch, parsed):
    _guest()
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=5))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"abc")
    assert client.get("/api/preview/tok").status_code == 403


def test_guest_can_revoke_own_preview(monkeypatch):
    """訪客上傳錯檔案時必須能主動銷毀，不能只能乾等 TTL。"""
    _guest()
    revoked = []
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=None))
    monkeypatch.setattr(pa, "revoke", lambda pid: revoked.append(pid) or True)
    assert client.delete("/api/preview/tok").status_code == 200
    assert revoked == ["tok"]


def test_guest_cannot_seal(monkeypatch):
    """seal 寫的是「某個具名的人確認過」；匿名封存等於沒有封存 → 必須登入。"""
    _guest()
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=None))
    monkeypatch.setattr(pa, "analyst_seal", lambda pid, uid: pytest.fail("訪客不應能 seal"))
    assert client.post("/api/preview/tok/seal").status_code == 401


# ── mapping-aware ───────────────────────────────────────────
def test_mapping_is_applied_and_persisted(created, parsed):
    _auth(USER)
    r = client.post(
        "/api/preview",
        files={"file": ("x.xlsx", b"abc", "application/octet-stream")},
        data={"target_id": "t", "mapping": '{"欄A":"time","欄B":"addr"}'},
    )
    assert r.status_code == 200, r.text
    assert parsed[0] == {"欄A": "time", "欄B": "addr"}          # 建立時就套用
    assert created["provenance"]["mapping"] == {"欄A": "time", "欄B": "addr"}
    # parser_type 要能區分「自動辨識」與「人工指定」——證據上兩者的可信度不同
    assert created["parser_type"] == "manual_mapping"
    assert r.json()["parser_type"] == "manual_mapping"


def test_no_mapping_keeps_auto_parser_type(created, parsed):
    _auth(USER)
    assert _post(target_id="t").status_code == 200
    assert parsed[0] is None
    assert created["parser_type"] == "auto"
    assert "mapping" not in created["provenance"]


@pytest.mark.parametrize("bad", ["{not json", '["a","b"]', '"x"'])
def test_malformed_mapping_rejected_400(created, parsed, bad):
    _auth(USER)
    r = client.post(
        "/api/preview",
        files={"file": ("x.xlsx", b"abc", "application/octet-stream")},
        data={"target_id": "t", "mapping": bad},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert parsed == []          # 壞 mapping 不進解析


def test_read_reuses_stored_mapping(monkeypatch, parsed):
    """read 必須用建立當下那份 mapping —— 否則同一個 artifact 兩次讀出不同結果。"""
    _auth(USER)
    prov = {"pipeline_version": "P9", "target_id": "t", "mapping": {"欄A": "time"}}
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(provenance=prov))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"abc")
    assert client.get("/api/preview/tok").status_code == 200
    assert parsed[0] == {"欄A": "time"}


def test_save_reuses_stored_mapping(monkeypatch, parsed):
    """save 走 server 重解析；mapping 必須跟著，否則手動對應的檔案會落地成 0 筆。"""
    _auth(USER)
    prov = {"pipeline_version": "P9", "target_id": "t", "mapping": {"欄A": "time"}}
    seen = {}
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(provenance=prov, sealed_at="x"))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"abc")
    monkeypatch.setattr(pa, "sha256_hex", lambda raw: "sha")
    monkeypatch.setattr(pa, "mark_consumed", lambda *a: True)
    monkeypatch.setattr("app.api.preview.register_evidence",
                        lambda **k: {"id": 99, "sha256_full": "sha", "size_bytes": 0, "prior_uploads": 0})
    monkeypatch.setattr("app.api.preview.update_evidence_stats", lambda *a: None)
    monkeypatch.setattr("app.api.preview.ingest_auto",
                        lambda *a, **kw: (seen.update(kw) or {"total": 1, "inserted": 1, "skipped": 0}))
    monkeypatch.setattr("app.api.preview.AUTH_ENABLED", False)
    r = client.post("/api/preview/tok/save", json={"project_id": "p1", "target_id": "t"})
    assert r.status_code == 200, r.text
    assert seen.get("mapping") == {"欄A": "time"}


def test_stored_mapping_ignores_non_dict(monkeypatch, parsed):
    """provenance 被塞了非 dict（舊資料 / 手改）時當作沒有 mapping，不可炸掉讀取。"""
    _auth(USER)
    prov = {"pipeline_version": "P9", "target_id": "t", "mapping": "oops"}
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(provenance=prov))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"abc")
    assert client.get("/api/preview/tok").status_code == 200
    assert parsed[0] is None
