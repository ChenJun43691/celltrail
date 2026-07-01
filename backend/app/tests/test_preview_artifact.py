# backend/app/tests/test_preview_artifact.py
"""
preview_artifact service 測試（P9A A.2，2026-07-01）。

策略：fake-cursor / monkeypatch，不依賴真 DB（CI 可跑）。
覆蓋：hashing（canonical JSON、禁 str）、storage routing、TTL、state_of、
create（db/object/too-large）、get_meta、load_raw、seal/consume/revoke、cleanup、
fail-closed（缺金鑰）。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

import app.services.preview_artifact as pa
from app.services.preview_artifact import (
    PreviewTooLargeError,
    PreviewStorageUnavailable,
    sha256_hex,
    canonical_records_hash,
    choose_storage_kind,
    state_of,
    _ttl,
)
from app.services import crypto_box

_VALID_KEY = "a1b2c3d4" * 8   # 64 hex → 32 bytes


# ── fake cursor / conn ────────────────────────────────────
class _FakeCursor:
    def __init__(self, fetch=None, rowcount=0):
        self.calls = []                # [(sql, params), ...]
        self._fetch = list(fetch or [])
        self.rowcount = rowcount

    def execute(self, sql, params=None, prepare=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._fetch.pop(0) if self._fetch else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, fetch=None, rowcount=0):
    """裝 fake get_conn + 跳過建表。回 fake cursor 供斷言。"""
    cur = _FakeCursor(fetch=fetch, rowcount=rowcount)
    monkeypatch.setattr(pa, "get_conn", lambda: _FakeConn(cur))
    monkeypatch.setattr(pa, "_table_ready", True)
    return cur


def _insert_call(cur):
    for sql, params in cur.calls:
        if "INSERT INTO preview_artifacts" in sql:
            return params
    raise AssertionError("找不到 INSERT INTO preview_artifacts 呼叫")


# ── hashing ────────────────────────────────────────────────
def test_sha256_hex_known_vector():
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_canonical_records_hash_deterministic():
    recs = [{"start_ts": "2026-06-28T05:20:00+00:00", "cell_id": "A", "lat": 22.6, "lng": 120.3}]
    assert canonical_records_hash(recs) == canonical_records_hash(recs)


def test_canonical_hash_dict_key_reorder_same():
    a = [{"a": 1, "b": 2, "c": 3}]
    b = [{"c": 3, "b": 2, "a": 1}]
    assert canonical_records_hash(a) == canonical_records_hash(b)


def test_canonical_hash_list_order_changes():
    r1 = {"t": 1}
    r2 = {"t": 2}
    assert canonical_records_hash([r1, r2]) != canonical_records_hash([r2, r1])


def test_canonical_hash_chinese_stable():
    recs = [{"cell_addr": "高雄市前金區中正四路211號", "cell_id": "466970108050142"}]
    h1 = canonical_records_hash(recs)
    h2 = canonical_records_hash([{"cell_id": "466970108050142", "cell_addr": "高雄市前金區中正四路211號"}])
    assert h1 == h2   # key 順序不同仍相同


def test_canonical_hash_not_str_based():
    recs = [{"b": 2, "a": 1}]
    str_based = hashlib.sha256(str(recs).encode("utf-8")).hexdigest()
    assert canonical_records_hash(recs) != str_based   # 證明未用 str(records)
    # 明確等於 canonical JSON 的 sha256
    canon = json.dumps(recs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert canonical_records_hash(recs) == hashlib.sha256(canon).hexdigest()


# ── storage routing ───────────────────────────────────────
def test_choose_storage_kind_db(monkeypatch):
    monkeypatch.setenv("PREVIEW_DB_MAX_MB", "5")
    monkeypatch.setenv("PREVIEW_MAX_MB", "50")
    assert choose_storage_kind(3 * 1024 * 1024) == "db"


def test_choose_storage_kind_object(monkeypatch):
    monkeypatch.setenv("PREVIEW_DB_MAX_MB", "5")
    monkeypatch.setenv("PREVIEW_MAX_MB", "50")
    assert choose_storage_kind(10 * 1024 * 1024) == "object"
    assert choose_storage_kind(5 * 1024 * 1024) == "object"   # 邊界：== DB_MAX → object


def test_choose_storage_kind_too_large(monkeypatch):
    monkeypatch.setenv("PREVIEW_MAX_MB", "50")
    with pytest.raises(PreviewTooLargeError):
        choose_storage_kind(51 * 1024 * 1024)


# ── TTL ────────────────────────────────────────────────────
def test_ttl_default(monkeypatch):
    monkeypatch.delenv("PREVIEW_TTL_MIN", raising=False)
    assert _ttl() == timedelta(minutes=30)


def test_ttl_invalid_fallback(monkeypatch):
    monkeypatch.setenv("PREVIEW_TTL_MIN", "abc")
    assert _ttl() == timedelta(minutes=30)


def test_ttl_out_of_range_fallback(monkeypatch):
    monkeypatch.setenv("PREVIEW_TTL_MIN", "999")
    assert _ttl() == timedelta(minutes=30)
    monkeypatch.setenv("PREVIEW_TTL_MIN", "45")
    assert _ttl() == timedelta(minutes=45)


# ── state_of ───────────────────────────────────────────────
def test_state_of_active():
    m = {"expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
    assert state_of(m) == "active"


def test_state_of_expired():
    m = {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}
    assert state_of(m) == "expired"


def test_state_of_revoked():
    m = {"revoked_at": datetime.now(timezone.utc), "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
    assert state_of(m) == "revoked"


def test_state_of_consumed():
    m = {"consumed_at": datetime.now(timezone.utc), "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
    assert state_of(m) == "consumed"


def test_state_of_not_found():
    assert state_of(None) == "not_found"


# ── create ─────────────────────────────────────────────────
def test_create_db_branch_encrypts(monkeypatch):
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", _VALID_KEY)
    monkeypatch.setenv("PREVIEW_DB_MAX_MB", "5")
    monkeypatch.setenv("PREVIEW_MAX_MB", "50")
    cur = _install(monkeypatch, fetch=[(123,)])

    raw = "時間,位置\n2026/06/28,高雄市".encode("utf-8")
    recs = [{"start_ts": "2026-06-28T00:00:00+00:00", "cell_addr": "高雄市"}]
    out = pa.create(raw=raw, records=recs, filename="x.xlsx", ext="xlsx",
                    parser_type="simple_time_location", provenance={"pipeline_version": "P9"},
                    created_by=7)

    assert out["id"] == 123
    assert out["storage_kind"] == "db"
    assert out["sha256_full"] == sha256_hex(raw)
    assert out["parsed_records_hash"] == canonical_records_hash(recs)
    assert out["row_count"] == 1
    assert len(out["preview_id"]) >= 20            # token_urlsafe(24)
    # INSERT 的 raw_enc 參數（index 8）可解密還原
    params = _insert_call(cur)
    raw_enc = params[8]
    assert crypto_box.decrypt_blob(raw_enc) == raw
    assert params[7] == "db"                       # storage_kind
    assert params[9] is None                       # storage_key（db 分支為 None）


def test_create_object_branch_stub(monkeypatch):
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", _VALID_KEY)
    monkeypatch.setenv("PREVIEW_DB_MAX_MB", "0")   # 令任何大小都落 object
    monkeypatch.setenv("PREVIEW_MAX_MB", "50")
    _install(monkeypatch, fetch=[(1,)])
    with pytest.raises(PreviewStorageUnavailable):
        pa.create(raw=b"x", records=[{"a": 1}], filename="x", ext=None,
                  parser_type="pdf", provenance={}, created_by=None)


def test_create_too_large(monkeypatch):
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", _VALID_KEY)
    monkeypatch.setenv("PREVIEW_MAX_MB", "0")      # 任何 >0 皆 too large
    _install(monkeypatch, fetch=[(1,)])
    with pytest.raises(PreviewTooLargeError):
        pa.create(raw=b"x", records=[{"a": 1}], filename="x", ext=None,
                  parser_type="pdf", provenance={}, created_by=None)


def test_create_missing_key_fail_closed(monkeypatch):
    monkeypatch.delenv("PREVIEW_ARTIFACT_KEY", raising=False)
    monkeypatch.setenv("PREVIEW_DB_MAX_MB", "5")
    monkeypatch.setenv("PREVIEW_MAX_MB", "50")
    _install(monkeypatch, fetch=[(1,)])
    with pytest.raises(crypto_box.PreviewKeyError):
        pa.create(raw=b"x", records=[{"a": 1}], filename="x", ext=None,
                  parser_type="pdf", provenance={}, created_by=None)


# ── get_meta / load_raw ────────────────────────────────────
def test_get_meta(monkeypatch):
    now = datetime.now(timezone.utc)
    row = (
        123, "tok123", "x.xlsx", "xlsx", 100, "sha", "prh", 5,
        "db", "aesgcm-v1", "simple_time_location",
        {"pipeline_version": "P9"}, 7, now, now + timedelta(minutes=30), now,
        None, None, None, None, None, None, None, None,
    )
    _install(monkeypatch, fetch=[row])
    meta = pa.get_meta("tok123")
    assert meta["preview_id"] == "tok123"
    assert meta["storage_kind"] == "db"
    assert meta["row_count"] == 5
    assert meta["parser_type"] == "simple_time_location"


def test_get_meta_not_found(monkeypatch):
    _install(monkeypatch, fetch=[None])
    assert pa.get_meta("nope") is None


def test_load_raw_db_branch(monkeypatch):
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", _VALID_KEY)
    raw = "回原始檔bytes".encode("utf-8")
    enc = crypto_box.encrypt_blob(raw)
    _install(monkeypatch, fetch=[("db", enc, None, "aesgcm-v1")])
    assert pa.load_raw("tok") == raw


def test_load_raw_not_found(monkeypatch):
    _install(monkeypatch, fetch=[None])
    with pytest.raises(KeyError):
        pa.load_raw("nope")


# ── seal / consume / revoke ────────────────────────────────
def test_analyst_seal(monkeypatch):
    cur = _install(monkeypatch, rowcount=1)
    assert pa.analyst_seal("tok", 9) is True
    sql, params = [c for c in cur.calls if "UPDATE preview_artifacts" in c[0]][0]
    assert "sealed_at = now()" in sql and "sealed_by" in sql
    assert params == (9, "tok")


def test_mark_consumed(monkeypatch):
    cur = _install(monkeypatch, rowcount=1)
    assert pa.mark_consumed("tok", "projX", "tgtY") is True
    sql, params = [c for c in cur.calls if "UPDATE preview_artifacts" in c[0]][0]
    assert "consumed_at = now()" in sql
    assert params == ("projX", "tgtY", "tok")


def test_revoke(monkeypatch):
    cur = _install(monkeypatch, rowcount=1)
    assert pa.revoke("tok") is True
    sql, params = [c for c in cur.calls if "UPDATE preview_artifacts" in c[0]][0]
    assert "revoked_at = now()" in sql
    assert params == ("tok",)


def test_seal_already_done_returns_false(monkeypatch):
    _install(monkeypatch, rowcount=0)   # WHERE sealed_at IS NULL 命中 0 列
    assert pa.analyst_seal("tok", 9) is False


# ── cleanup ────────────────────────────────────────────────
def test_cleanup_expired(monkeypatch):
    cur = _install(monkeypatch, rowcount=3)
    assert pa.cleanup_expired() == 3
    assert any("DELETE FROM preview_artifacts" in sql and "expires_at < now()" in sql
               for sql, _ in cur.calls)
