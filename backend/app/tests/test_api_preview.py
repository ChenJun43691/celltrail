# backend/app/tests/test_api_preview.py
"""
Preview API（A.3）端點測試（P9A，2026-07-02）。

TestClient + dependency_overrides（auth）+ monkeypatch（service/ingest/evidence/audit）；
不碰真 DB/geocode。驗端點契約、ACL、狀態機、sha256 gate、inline seal、audit、無 _records。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

from fastapi.testclient import TestClient

import app.main as main_mod
import app.services.preview_artifact as pa
from app.security import get_current_user
from app.services.crypto_box import PreviewKeyError
from app.services.preview_artifact import PreviewTooLargeError

app = main_mod.app
client = TestClient(app)

ADMIN = {"id": 1, "username": "admin", "role": "admin"}
OWNER = {"id": 5, "username": "u5", "role": "user"}
OTHER = {"id": 6, "username": "u6", "role": "user"}

_RECS = [{
    "target_id": "t", "start_ts": "2026-06-28T00:00:00+00:00", "end_ts": "2026-06-28T00:05:00+00:00",
    "cell_id": "A", "cell_addr": "高雄市", "sector_name": None, "site_code": None, "sector_id": None,
    "azimuth": None, "lat": 22.6, "lng": 120.3, "accuracy_m": 150, "azimuth_ref": "unknown",
}]


def _meta(**over):
    base = {
        "id": 10, "preview_id": "tok", "filename": "x.xlsx", "ext": "xlsx",
        "sha256_full": "sha", "row_count": 1, "parser_type": "auto",
        "provenance": {"pipeline_version": "P9", "target_id": "t"}, "created_by": 5,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "system_sealed_at": datetime.now(timezone.utc),
        "sealed_at": None, "sealed_by": None, "consumed_at": None,
        "consumed_project": None, "consumed_target": None, "revoked_at": None,
    }
    base.update(over)
    return base


def _auth(user):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def audits(monkeypatch):
    calls = []
    monkeypatch.setattr("app.api.preview.write_audit", lambda **kw: calls.append(kw) or 1)
    return calls


@pytest.fixture
def svc(monkeypatch):
    """預設 mock artifact/ingest/evidence 呼叫，回追蹤 dict。"""
    calls = {"analyst_seal": [], "mark_consumed": [], "revoke": [],
             "register_evidence": [], "ingest_auto": []}
    monkeypatch.setattr(pa, "analyst_seal", lambda pid, uid: (calls["analyst_seal"].append((pid, uid)) or True))
    monkeypatch.setattr(pa, "mark_consumed", lambda pid, p, t: (calls["mark_consumed"].append((pid, p, t)) or True))
    monkeypatch.setattr(pa, "revoke", lambda pid: (calls["revoke"].append(pid) or True))
    monkeypatch.setattr("app.api.preview.register_evidence",
                        lambda **k: (calls["register_evidence"].append(k) or {"id": 99, "sha256_full": "sha", "size_bytes": 0, "prior_uploads": 0}))
    monkeypatch.setattr("app.api.preview.ingest_auto",
                        lambda *a: (calls["ingest_auto"].append(a) or {"total": 1, "inserted": 1, "skipped": 0, "errors": []}))
    monkeypatch.setattr("app.api.preview.update_evidence_stats", lambda *a: None)
    return calls


def _actions(audits):
    return [c.get("action") for c in audits]


# ── POST /api/preview ───────────────────────────────────────
def test_post_success(monkeypatch, audits):
    _auth(ADMIN)
    monkeypatch.setattr("app.api.preview.parse_file_only", lambda *a, **k: list(_RECS))
    monkeypatch.setattr(pa, "create", lambda **k: {
        "preview_id": "tok_abc", "sha256_full": "sha", "size_bytes": 3, "storage_kind": "db",
        "row_count": 1, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
    })
    r = client.post("/api/preview", files={"file": ("x.xlsx", b"abc", "application/octet-stream")}, data={"target_id": "t"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["preview_id"] == "tok_abc"
    assert j["total"] == 1 and j["plotted"] == 1 and j["skipped"] == 0
    assert "_records" not in j
    assert "preview.create" in _actions(audits)


def test_post_too_large(monkeypatch, audits):
    _auth(ADMIN)
    monkeypatch.setenv("PREVIEW_MAX_MB", "0")   # 任何 >0 → too large
    r = client.post("/api/preview", files={"file": ("x.xlsx", b"abcdef", "application/octet-stream")})
    assert r.status_code == 413


def test_post_object_stub_413(monkeypatch, audits):
    _auth(ADMIN)
    monkeypatch.setenv("PREVIEW_DB_MAX_MB", "0")   # 任何大小 → object 分支
    monkeypatch.setenv("PREVIEW_MAX_MB", "50")
    r = client.post("/api/preview", files={"file": ("x.xlsx", b"abcdef", "application/octet-stream")})
    assert r.status_code == 413


def test_post_missing_key_503(monkeypatch, audits):
    _auth(ADMIN)
    monkeypatch.delenv("PREVIEW_DB_MAX_MB", raising=False)
    monkeypatch.delenv("PREVIEW_MAX_MB", raising=False)
    monkeypatch.setattr("app.api.preview.parse_file_only", lambda *a, **k: list(_RECS))

    def _raise(**k):
        raise PreviewKeyError("no key")
    monkeypatch.setattr(pa, "create", _raise)
    r = client.post("/api/preview", files={"file": ("x.xlsx", b"abc", "application/octet-stream")})
    assert r.status_code == 503


# ── GET /api/preview/{id} ───────────────────────────────────
def test_get_active_success(monkeypatch, audits, svc):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta())
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"raw")
    monkeypatch.setattr("app.api.preview.parse_file_only", lambda *a, **k: list(_RECS))
    r = client.get("/api/preview/tok")
    assert r.status_code == 200
    j = r.json()
    assert j["plotted"] == 1 and "_records" not in j
    assert "preview.read" in _actions(audits)
    # pure read：不得動 artifact 狀態
    assert svc["analyst_seal"] == [] and svc["mark_consumed"] == [] and svc["revoke"] == []


def test_read_rebuild_failure_no_raw_exception_message(monkeypatch, audits, caplog):
    """rebuild 失敗時，底層 exception message（可能含 JWT/密碼/連線字串/中文地址/raw bytes）
    不得進入 audit error_text、structured log 或 response —— 只留 exception class + 固定安全摘要。
    """
    import json as _json
    import logging as _logging

    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta())
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"raw")

    # 刻意讓底層 exception message 塞滿各類敏感內容
    SENSITIVE = (
        "eyJhbGciOiJIUzI1NiJ9.PAYLOAD.SIGNATURE "          # JWT 形狀
        "password=p@ssw0rd! "
        "postgresql://celltrail:supersecret@db.internal:5432/celltrail "
        "高雄市前金區中正四路211號 "
        "rawbytes=\\x00\\x01\\x02"
    )

    def _boom(*a, **k):
        raise RuntimeError(SENSITIVE)
    monkeypatch.setattr("app.api.preview.parse_file_only", _boom)

    with caplog.at_level(_logging.INFO, logger="celltrail"):
        r = client.get("/api/preview/tok")

    # 1. response contract：固定訊息、不洩漏
    assert r.status_code == 500
    b = r.json()
    assert b["error"]["code"] == "PREVIEW_PARSE_FAILED"
    assert b["error"]["message"] == "預覽重建失敗，請重新上傳。"
    for bad in ("eyJhbGci", "p@ssw0rd", "supersecret", "postgresql://", "中正四路", "rawbytes"):
        assert bad not in r.text

    # 2. audit payload：error_text 只留固定摘要 + class 名，不含 str(e)
    rebuild = [c for c in audits if c.get("action") == "preview.read" and c.get("status_code") == 500]
    assert len(rebuild) == 1
    et = rebuild[0].get("error_text") or ""
    assert et == "preview rebuild failed: RuntimeError"
    audit_dump = _json.dumps(rebuild[0], ensure_ascii=False, default=str)
    for bad in ("eyJhbGci", "p@ssw0rd", "supersecret", "postgresql://", "中正四路", "rawbytes"):
        assert bad not in audit_dump

    # 3. structured logs：既無 str(e)，且 domain + 全域兩筆皆乾淨
    evts = []
    for rec in caplog.records:
        if rec.name == "celltrail":
            try:
                evts.append(_json.loads(rec.getMessage()))
            except Exception:
                pass
    dumped = _json.dumps(evts, ensure_ascii=False)
    for bad in ("eyJhbGci", "p@ssw0rd", "supersecret", "postgresql://", "中正四路", "rawbytes"):
        assert bad not in dumped
    domain = [e for e in evts if e.get("event") == "preview.read.rebuild_failed"]
    assert domain and domain[-1]["error_type"] == "RuntimeError"
    assert domain[-1]["error_stage"] == "preview_rebuild"
    assert "message" not in domain[-1]      # 未夾帶 exception message 欄位
    server = [e for e in evts if e.get("event") == "app.error.server"]
    assert server and server[-1]["status_code"] == 500


def test_get_not_found(monkeypatch, audits):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: None)
    assert client.get("/api/preview/nope").status_code == 404


@pytest.mark.parametrize("over,label", [
    ({"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}, "expired"),
    ({"revoked_at": datetime.now(timezone.utc)}, "revoked"),
    ({"consumed_at": datetime.now(timezone.utc)}, "consumed"),
])
def test_get_inactive_410(monkeypatch, audits, over, label):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(**over))
    assert client.get("/api/preview/tok").status_code == 410


def test_get_forbidden(monkeypatch, audits):
    _auth(OTHER)   # id 6，非 owner（created_by 5），非 admin
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=5))
    assert client.get("/api/preview/tok").status_code == 403


# ── seal ────────────────────────────────────────────────────
def test_seal_success(monkeypatch, audits, svc):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta())
    r = client.post("/api/preview/tok/seal")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert svc["analyst_seal"] and "preview.seal" in _actions(audits)


# ── save ────────────────────────────────────────────────────
def test_save_success_inline_seal(monkeypatch, audits, svc):
    _auth(ADMIN)   # admin 跳過 project 權限檢查
    raw = b"raw-bytes-authoritative"
    sha = pa.sha256_hex(raw)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(sha256_full=sha, sealed_at=None))
    monkeypatch.setattr(pa, "load_raw", lambda pid: raw)
    r = client.post("/api/preview/tok/save", json={"project_id": "P", "target_id": "T"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["evidence_id"] == 99 and j["inserted"] == 1
    assert "_records" not in j
    assert svc["register_evidence"] and svc["ingest_auto"] and svc["mark_consumed"]
    acts = _actions(audits)
    assert "preview.seal" in acts and "preview.consume" in acts   # inline seal + consume


def test_save_sha_mismatch_409(monkeypatch, audits, svc):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(sha256_full="WRONGHASH"))
    monkeypatch.setattr(pa, "load_raw", lambda pid: b"raw")
    r = client.post("/api/preview/tok/save", json={"project_id": "P", "target_id": "T"})
    assert r.status_code == 409
    assert svc["register_evidence"] == [] and svc["ingest_auto"] == []   # 未落地


def test_save_requires_project_access(monkeypatch, audits, svc):
    _auth(OWNER)   # 非 admin，但為 preview owner（created_by 5）
    raw = b"raw"
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=5, sha256_full=pa.sha256_hex(raw)))
    monkeypatch.setattr(pa, "load_raw", lambda pid: raw)
    monkeypatch.setattr("app.api.preview.project_has_members", lambda p: True)
    from fastapi import HTTPException

    def _deny(*a, **k):
        raise HTTPException(status_code=403, detail="無此案件的存取權限")
    monkeypatch.setattr("app.api.preview.assert_project_access", _deny)
    r = client.post("/api/preview/tok/save", json={"project_id": "P", "target_id": "T"})
    assert r.status_code == 403
    assert svc["ingest_auto"] == []


def test_save_consumed_410(monkeypatch, audits, svc):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(consumed_at=datetime.now(timezone.utc)))
    r = client.post("/api/preview/tok/save", json={"project_id": "P", "target_id": "T"})
    assert r.status_code == 410


# ── delete ──────────────────────────────────────────────────
def test_delete_success(monkeypatch, audits, svc):
    _auth(ADMIN)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta())
    r = client.delete("/api/preview/tok")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert svc["revoke"] == ["tok"] and "preview.delete" in _actions(audits)


def test_delete_forbidden(monkeypatch, audits, svc):
    _auth(OTHER)
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(created_by=5))
    r = client.delete("/api/preview/tok")
    assert r.status_code == 403
    assert svc["revoke"] == []


# ── 全端點回應不含 _records（彙整）───────────────────────────
def test_no_records_in_any_response(monkeypatch, audits, svc):
    _auth(ADMIN)
    raw = b"raw"
    monkeypatch.setattr(pa, "get_meta", lambda pid: _meta(sha256_full=pa.sha256_hex(raw)))
    monkeypatch.setattr(pa, "load_raw", lambda pid: raw)
    monkeypatch.setattr("app.api.preview.parse_file_only", lambda *a, **k: list(_RECS))
    monkeypatch.setattr(pa, "create", lambda **k: {
        "preview_id": "tok", "sha256_full": "s", "size_bytes": 3, "storage_kind": "db",
        "row_count": 1, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30)})
    for resp in [
        client.post("/api/preview", files={"file": ("x.xlsx", b"abc", "application/octet-stream")}),
        client.get("/api/preview/tok"),
        client.post("/api/preview/tok/seal"),
        client.post("/api/preview/tok/save", json={"project_id": "P", "target_id": "T"}),
        client.delete("/api/preview/tok"),
    ]:
        assert "_records" not in resp.text
