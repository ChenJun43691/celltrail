# backend/app/services/evidence.py
"""
證物（evidence_files）服務 —— 上傳檔案的不可竄改指紋封存。

設計準則：
  - 在「實際 ingest 之前」算 SHA-256（避免 ingest 失敗就沒 hash）
  - 同 sha256_full 重複上傳「不阻擋」（事證可能被合法重提交），但會在 audit 留紀錄
  - 永遠 INSERT，不 UPDATE / DELETE（與 audit_logs 同 append-only 原則）

法庭採信意義：
  1. 全檔案 SHA-256 = 「這份檔案在進入系統時的 byte-for-byte 指紋」
  2. 任何後續對 raw_traces 的解析錯誤、欄位修正，都可回頭驗證原始檔案 hash 未動
  3. 配合 audit_logs，可重建「誰、何時、把什麼指紋的檔案，匯入到哪個 project/target」
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from app.db.session import get_conn


def sha256_full(content: bytes) -> str:
    """全檔案 SHA-256（hex 64 字元）"""
    return hashlib.sha256(content).hexdigest()


def register_evidence(
    *,
    project_id: str,
    target_id: str,
    filename: str,
    ext: Optional[str],
    content: bytes,
    mime_hint: Optional[str] = None,
    uploaded_by: Optional[int] = None,
    uploaded_by_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    在 ingest 前呼叫；計算 SHA-256 並寫入 evidence_files。
    回傳 dict：{ id, sha256_full, size_bytes, prior_uploads }

    prior_uploads：之前是否曾上傳過同 sha256 的檔案（任何 project / target / 使用者皆計）。
                   給 audit_logs 一個欄位記「這份證物之前有沒有出現過」。
    """
    sha = sha256_full(content)
    size = len(content)

    sql_insert = """
    INSERT INTO evidence_files (
        project_id, target_id,
        filename, ext, size_bytes, sha256_full, mime_hint,
        uploaded_by, uploaded_by_name
    ) VALUES (
        %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s
    )
    RETURNING id
    """
    sql_count_prior = """
    SELECT COUNT(*) FROM evidence_files
     WHERE sha256_full = %s
    """

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql_count_prior, (sha,), prepare=False)
        prior = int(cur.fetchone()[0])

        cur.execute(
            sql_insert,
            (project_id, target_id,
             filename, ext, size, sha, mime_hint,
             uploaded_by, uploaded_by_name),
            prepare=False,
        )
        row = cur.fetchone()
        new_id = int(row[0]) if row else None

    return {
        "id":            new_id,
        "sha256_full":   sha,
        "size_bytes":    size,
        "prior_uploads": prior,   # 不含本次；>=1 代表這份檔案以前進過系統
    }


def update_evidence_stats(
    evidence_id: int,
    rows_total: int,
    rows_inserted: int,
    rows_skipped: int,
) -> None:
    """
    Ingest 完成後回填統計。為什麼這個算「破例 UPDATE」：
      - sha256/filename/uploaded_by 等核心鑑識欄位 INSERT 後就鎖死
      - rows_* 是 ingest 副產物，等 ingest 跑完才知道，要回填一次
      - 這個 UPDATE 不會改任何鑑識用欄位，只回寫統計，不違反鑑識完整性
    """
    sql = """
    UPDATE evidence_files
       SET rows_total = %s,
           rows_inserted = %s,
           rows_skipped = %s
     WHERE id = %s
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (rows_total, rows_inserted, rows_skipped, evidence_id),
                        prepare=False)
    except Exception as e:
        # 統計回填失敗不應拖垮上傳，僅 print
        print(f"[evidence] WARN 統計回填失敗 id={evidence_id}: {type(e).__name__}: {e}")
