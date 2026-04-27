# backend/app/services/audit.py
"""
Audit Log 服務（P0 法庭可防禦性核心）
============================================================
寫入專用的 helper：append-only ledger，所有業務動作都應透過 write_audit() 落地。

為何單獨拉一支 service 而不是散落於各端點：
  1. 統一 hash / IP / UA 的取得方式，避免各端點各自寫入欄位不一致
  2. 把「audit 失敗不應拖垮業務 API」這個原則包在這裡（safe=True 預設）
  3. 將來要把 audit log 鏡像到 SIEM（如 ELK / Splunk）時只動這一支
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from fastapi import Request

from app.db.session import get_conn


# ── 內部工具 ────────────────────────────────────────────────────

def _client_ip(request: Optional[Request]) -> Optional[str]:
    """
    取出 client IP。優先順序：
      X-Forwarded-For（反向代理） → X-Real-IP → request.client.host
    """
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # XFF 可能是 "client, proxy1, proxy2"，取第一個
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    return request.headers.get("user-agent")


def _json_default(o: Any):
    """讓 datetime / Decimal / set 都能被 json.dumps 處理"""
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if isinstance(o, set):
        return list(o)
    return str(o)


def _hash_payload(details: Dict[str, Any]) -> str:
    """以 SHA-256(canonical-json(details)) 作為事後驗證錨點"""
    blob = json.dumps(details, ensure_ascii=False, sort_keys=True, default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── 公開 API ────────────────────────────────────────────────────

def write_audit(
    *,
    action: str,
    user: Optional[Dict[str, Any]] = None,
    target_type: Optional[str] = None,
    target_ref: Optional[str] = None,
    project_id: Optional[str] = None,
    request: Optional[Request] = None,
    details: Optional[Dict[str, Any]] = None,
    status_code: Optional[int] = None,
    error_text: Optional[str] = None,
    safe: bool = True,
) -> Optional[int]:
    """
    寫入一筆 audit log；回傳新紀錄的 id（失敗時 None）。

    參數
    ----
    action      : 動作別字串。建議使用以下命名：
                  upload | upload_failed | delete_target | restore_target
                  | update_user | create_user | login | login_failed | export
    user        : 從 Depends(get_current_user) 拿到的 dict；含 id / username / role
    target_type : 標的類別字串。例：'raw_traces' | 'target' | 'project' | 'user'
    target_ref  : 標的業務鍵（用字串保存，避免 FK 拖累 audit）。
    project_id  : 對應的 project_id（多數查詢都會以此欄位 filter）
    request     : FastAPI Request 物件（拿 IP / UA 用）
    details     : 任意 JSON 可序列化 dict
    status_code : 對應 HTTP-like 狀態碼（200 / 400 / 500）
    error_text  : 失敗時的錯誤訊息
    safe        : True（預設）→ audit 寫入失敗不會拋例外，僅 print warning。
                  False → 失敗時直接 raise，呼叫端自行處理。

    為什麼 safe 預設 True：
      審計紀錄缺一筆，比讓使用者上傳失敗更可接受。但 production 應監控
      [audit] WARN log，避免 audit 持續寫不進。
    """
    details = dict(details or {})

    user_id   = (user or {}).get("id")
    username  = (user or {}).get("username")
    role      = (user or {}).get("role")
    ip        = _client_ip(request)
    user_agent= _user_agent(request)
    payload_h = _hash_payload(details)

    sql = """
    INSERT INTO audit_logs (
        user_id, username, role,
        action, target_type, target_ref, project_id,
        ip, user_agent,
        details, payload_hash,
        status_code, error_text
    ) VALUES (
        %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s,
        %s::jsonb, %s,
        %s, %s
    )
    RETURNING id
    """
    payload_json = json.dumps(details, ensure_ascii=False, default=_json_default)
    params = (
        user_id, username, role,
        action, target_type, target_ref, project_id,
        ip, user_agent,
        payload_json, payload_h,
        status_code, error_text,
    )
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params, prepare=False)
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        msg = f"[audit] WARN 寫入失敗 action={action} err={type(e).__name__}: {e}"
        if safe:
            print(msg)
            return None
        raise
