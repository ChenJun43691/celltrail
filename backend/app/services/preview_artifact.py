# backend/app/services/preview_artifact.py
"""
Preview artifact 服務（P9A A.2，2026-07-01）。

負責 preview_artifacts 的生命週期：建立（加密存原始檔 + 雙 hash + provenance）、
讀取 metadata、載回原始 bytes（解密）、seal/consume/revoke、TTL 清理。

設計鎖定（本輪 + 前輪決策）：
  - internal id = BIGSERIAL PK；external identifier = preview_id（token_urlsafe(24)）。
    對外一律用 preview_id；FK/內部主鍵用 id（token 不當 FK）。
  - Hybrid storage：<PREVIEW_DB_MAX_MB → 'db'（crypto_box 加密存 BYTEA）；
    [DB_MAX, MAX] → 'object'（A.2 stub，raise PreviewStorageUnavailable）；
    > PREVIEW_MAX_MB → PreviewTooLargeError。
  - parsed_records_hash = canonical JSON sha256（禁 str/repr，見 canonical_records_hash）。
  - system_sealed_at 於 create 寫入（系統對 sha256+parsed_hash 之背書時刻）。
  - 無 read_count（讀取次數改由 audit_logs 導出）；GET 於 API 層 pure read。
  - 所有 cur.execute 帶 prepare=False（pooler 約束）。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.db.session import get_conn
from app.services import crypto_box

# ── env 預設 ───────────────────────────────────────────────
_DEFAULT_TTL_MIN = 30
_DEFAULT_DB_MAX_MB = 5
_DEFAULT_MAX_MB = 50
_MB = 1024 * 1024


class PreviewTooLargeError(Exception):
    """檔案超過 PREVIEW_MAX_MB，禁止建 preview（API 層轉 413，引導走正式 /upload）。"""


class PreviewStorageUnavailable(Exception):
    """object storage 分支（A.2 stub，尚未實作；API 層轉 503）。"""


# ── env helpers ────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _ttl() -> timedelta:
    """preview 存活時間；env PREVIEW_TTL_MIN（預設 30，合理 [5,120]，非法/超範圍 fallback 30）。"""
    m = _env_int("PREVIEW_TTL_MIN", _DEFAULT_TTL_MIN)
    if m < 5 or m > 120:
        m = _DEFAULT_TTL_MIN
    return timedelta(minutes=m)


def _db_max_bytes() -> int:
    return _env_int("PREVIEW_DB_MAX_MB", _DEFAULT_DB_MAX_MB) * _MB


def _max_bytes() -> int:
    return _env_int("PREVIEW_MAX_MB", _DEFAULT_MAX_MB) * _MB


# ── 純函式（hashing / routing / state）──────────────────────
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def canonical_records_hash(records: List[Dict[str, Any]]) -> str:
    """records 的 canonical-JSON SHA-256。

    **鎖定**：deterministic 序列化——dict key 排序、無多餘空白、保 UTF-8。
    **禁**：str(records) / repr(records) 或任何非 deterministic 序列化。
    語意：list 順序有意義（列=時序）；dict key 順序無意義（sort_keys 正規化）。
    """
    canon = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def choose_storage_kind(size_bytes: int) -> str:
    """依大小決定儲存位置；> PREVIEW_MAX_MB → PreviewTooLargeError。"""
    if size_bytes > _max_bytes():
        raise PreviewTooLargeError(
            f"檔案 {size_bytes} bytes 超過 preview 上限 {_max_bytes()} bytes，請改用正式上傳。"
        )
    if size_bytes < _db_max_bytes():
        return "db"
    return "object"


def state_of(meta: Optional[Dict[str, Any]]) -> str:
    """回 'not_found'|'revoked'|'consumed'|'expired'|'active'（供 API 決定 404/410/200）。"""
    if not meta:
        return "not_found"
    if meta.get("revoked_at"):
        return "revoked"
    if meta.get("consumed_at"):
        return "consumed"
    exp = meta.get("expires_at")
    if exp is not None and exp < datetime.now(timezone.utc):
        return "expired"
    return "active"


# ── object storage 分支（A.2 stub）─────────────────────────
def _store_object(raw: bytes) -> str:
    raise PreviewStorageUnavailable("object storage 分支尚未實作（A.5）；請改用正式 /upload。")


def _load_object(storage_key: str) -> bytes:
    raise PreviewStorageUnavailable("object storage 分支尚未實作（A.5）。")


# ── 建表（idempotent；仿 geocode._ensure_sql_cache）─────────
_table_ready = False

_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS preview_artifacts (
  id                    BIGSERIAL   PRIMARY KEY,
  preview_id            TEXT UNIQUE NOT NULL,
  filename              TEXT        NOT NULL,
  ext                   TEXT        NULL,
  size_bytes            BIGINT      NOT NULL,
  sha256_full           TEXT        NOT NULL,
  parsed_records_hash   TEXT        NOT NULL,
  row_count             INT         NOT NULL,
  storage_kind          TEXT        NOT NULL,
  raw_enc               BYTEA       NULL,
  storage_key           TEXT        NULL,
  enc_alg               TEXT        NOT NULL,
  parser_type           TEXT        NOT NULL,
  provenance            JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_by            BIGINT      NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at            TIMESTAMPTZ NOT NULL,
  system_sealed_at      TIMESTAMPTZ NULL,
  sealed_at             TIMESTAMPTZ NULL,
  sealed_by             BIGINT      NULL,
  supervisor_sealed_at  TIMESTAMPTZ NULL,
  supervisor_sealed_by  BIGINT      NULL,
  consumed_at           TIMESTAMPTZ NULL,
  consumed_project      TEXT        NULL,
  consumed_target       TEXT        NULL,
  revoked_at            TIMESTAMPTZ NULL
)
"""
_DDL_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_preview_artifacts_pid     ON preview_artifacts (preview_id)",
    "CREATE INDEX        IF NOT EXISTS idx_preview_artifacts_expires ON preview_artifacts (expires_at)",
    "CREATE INDEX        IF NOT EXISTS idx_preview_artifacts_creator ON preview_artifacts (created_by)",
]


def _ensure_preview_table() -> bool:
    global _table_ready
    if _table_ready:
        return True
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(_DDL_TABLE, prepare=False)
            for ddl in _DDL_INDEXES:
                cur.execute(ddl, prepare=False)
            conn.commit()
        _table_ready = True
    except Exception as e:
        print(f"[preview_artifact] ensure table failed: {type(e).__name__}: {e}")
    return _table_ready


# ── metadata 欄位（get_meta 的 SELECT 順序）────────────────
_META_COLS = [
    "id", "preview_id", "filename", "ext", "size_bytes", "sha256_full",
    "parsed_records_hash", "row_count", "storage_kind", "enc_alg", "parser_type",
    "provenance", "created_by", "created_at", "expires_at", "system_sealed_at",
    "sealed_at", "sealed_by", "supervisor_sealed_at", "supervisor_sealed_by",
    "consumed_at", "consumed_project", "consumed_target", "revoked_at",
]


# ── 生命週期 ───────────────────────────────────────────────
def create(
    *,
    raw: bytes,
    records: List[Dict[str, Any]],
    filename: str,
    ext: Optional[str],
    parser_type: str,
    provenance: Dict[str, Any],
    created_by: Optional[int],
) -> Dict[str, Any]:
    """建立 preview artifact：加密存原始檔 + sha256_full + parsed_records_hash + provenance。

    - size > PREVIEW_MAX_MB → PreviewTooLargeError
    - object 分支（5–50MB）→ PreviewStorageUnavailable（A.2 stub）
    - 金鑰缺失 → crypto_box.PreviewKeyError（fail-closed）
    回 dict（不含 raw）：preview_id / sha256_full / parsed_records_hash / row_count /
    size_bytes / storage_kind / expires_at / system_sealed_at / id。
    """
    size = len(raw)
    kind = choose_storage_kind(size)                # 可能 raise PreviewTooLargeError
    sha = sha256_hex(raw)
    prh = canonical_records_hash(records)
    row_count = len(records)
    preview_id = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + _ttl()

    raw_enc: Optional[bytes] = None
    storage_key: Optional[str] = None
    enc_alg = crypto_box.ENC_ALG
    if kind == "db":
        raw_enc = crypto_box.encrypt_blob(raw)      # 可能 raise PreviewKeyError（fail-closed）
    else:
        storage_key = _store_object(raw)            # A.2 stub → PreviewStorageUnavailable

    _ensure_preview_table()
    sql = """
    INSERT INTO preview_artifacts (
      preview_id, filename, ext, size_bytes, sha256_full, parsed_records_hash, row_count,
      storage_kind, raw_enc, storage_key, enc_alg,
      parser_type, provenance, created_by, expires_at, system_sealed_at
    ) VALUES (
      %s,%s,%s,%s,%s,%s,%s,
      %s,%s,%s,%s,
      %s,%s::jsonb,%s,%s,%s
    ) RETURNING id
    """
    params = (
        preview_id, filename, ext, size, sha, prh, row_count,
        kind, raw_enc, storage_key, enc_alg,
        parser_type, json.dumps(provenance, ensure_ascii=False), created_by, expires, now,
    )
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        rid = int(cur.fetchone()[0])
        conn.commit()

    return {
        "id": rid,
        "preview_id": preview_id,
        "sha256_full": sha,
        "parsed_records_hash": prh,
        "row_count": row_count,
        "size_bytes": size,
        "storage_kind": kind,
        "expires_at": expires,
        "system_sealed_at": now,
    }


def get_meta(preview_id: str) -> Optional[Dict[str, Any]]:
    """依 preview_id 查 metadata（不含 raw_enc/storage_key）。找不到回 None。"""
    _ensure_preview_table()
    sql = f"SELECT {', '.join(_META_COLS)} FROM preview_artifacts WHERE preview_id = %s"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (preview_id,), prepare=False)
        row = cur.fetchone()
    if not row:
        return None
    return dict(zip(_META_COLS, row))


def load_raw(preview_id: str) -> bytes:
    """載回原始 bytes（db → 解密；object → stub）。找不到 → KeyError。"""
    _ensure_preview_table()
    sql = "SELECT storage_kind, raw_enc, storage_key, enc_alg FROM preview_artifacts WHERE preview_id = %s"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (preview_id,), prepare=False)
        row = cur.fetchone()
    if not row:
        raise KeyError(f"preview not found: {preview_id}")
    kind, raw_enc, storage_key, _enc_alg = row
    if kind == "db":
        return crypto_box.decrypt_blob(bytes(raw_enc))
    return _load_object(storage_key)                # A.2 stub


def analyst_seal(preview_id: str, user_id: Optional[int]) -> bool:
    """Analyst seal（第一次有效）。回 True 表本次寫入。"""
    _ensure_preview_table()
    sql = """
    UPDATE preview_artifacts
       SET sealed_at = now(), sealed_by = %s
     WHERE preview_id = %s AND sealed_at IS NULL
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (user_id, preview_id), prepare=False)
        conn.commit()
        return cur.rowcount > 0


def mark_consumed(preview_id: str, project_id: str, target_id: str) -> bool:
    """標記已 persist。回 True 表本次寫入。"""
    _ensure_preview_table()
    sql = """
    UPDATE preview_artifacts
       SET consumed_at = now(), consumed_project = %s, consumed_target = %s
     WHERE preview_id = %s AND consumed_at IS NULL
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id, target_id, preview_id), prepare=False)
        conn.commit()
        return cur.rowcount > 0


def revoke(preview_id: str) -> bool:
    """撤銷（設 revoked_at）。回 True 表本次寫入。"""
    _ensure_preview_table()
    sql = "UPDATE preview_artifacts SET revoked_at = now() WHERE preview_id = %s AND revoked_at IS NULL"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (preview_id,), prepare=False)
        conn.commit()
        return cur.rowcount > 0


def cleanup_expired() -> int:
    """刪除所有過期 artifact（含 object，stub）。回刪除筆數。"""
    _ensure_preview_table()
    sql = "DELETE FROM preview_artifacts WHERE expires_at < now()"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, prepare=False)
        n = cur.rowcount
        conn.commit()
    return int(n or 0)
