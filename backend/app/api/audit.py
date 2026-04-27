# backend/app/api/audit.py
"""
Audit Log 查詢 API（唯讀；無 POST/PUT/DELETE）

設計考量：
  - 不允許任何 mutation 端點（append-only ledger 由 services/audit.py 寫入）
  - 限管理員存取（require_admin）；偵查員可看自己的記錄但管理員可看全部
  - 預設僅回 100 筆，最大 1000 筆，避免 dump 整張表
"""
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, Query

from app.db.session import get_conn
from app.security import get_current_user, require_admin

router = APIRouter()


@router.get(
    "/audit/logs",
    dependencies=[Depends(require_admin)],
)
def list_audit_logs(
    project_id: Optional[str] = None,
    target_ref: Optional[str] = None,
    user_id:    Optional[int] = None,
    action:     Optional[str] = None,
    since:      Optional[datetime] = Query(None, description="ts >= since (ISO-8601)"),
    until:      Optional[datetime] = Query(None, description="ts <  until (ISO-8601)"),
    page:       int = Query(1, ge=1),
    page_size:  int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """
    依條件查 audit_logs。回傳：
        { "page": int, "page_size": int, "total": int, "items": [...] }
    """
    where: List[str] = []
    params: List[Any] = []

    if project_id:
        where.append("project_id = %s"); params.append(project_id)
    if target_ref:
        where.append("target_ref = %s"); params.append(target_ref)
    if user_id is not None:
        where.append("user_id = %s"); params.append(user_id)
    if action:
        where.append("action = %s"); params.append(action)
    if since:
        where.append("ts >= %s"); params.append(since)
    if until:
        where.append("ts <  %s"); params.append(until)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    count_sql = f"SELECT COUNT(*) FROM audit_logs {where_sql}"
    list_sql = f"""
    SELECT
        id, ts, user_id, username, role,
        action, target_type, target_ref, project_id,
        ip, user_agent,
        details, payload_hash,
        status_code, error_text
    FROM audit_logs
    {where_sql}
    ORDER BY ts DESC, id DESC
    LIMIT %s OFFSET %s
    """
    offset = (page - 1) * page_size

    items: List[Dict[str, Any]] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(count_sql, params, prepare=False)
        total = int(cur.fetchone()[0])

        cur.execute(list_sql, [*params, page_size, offset], prepare=False)
        for row in cur.fetchall():
            (rid, ts, uid, uname, role,
             act, ttype, tref, pid,
             ip, ua,
             details, ph,
             sc, err) = row
            items.append({
                "id":           rid,
                "ts":           ts.isoformat() if ts else None,
                "user_id":      uid,
                "username":     uname,
                "role":         role,
                "action":       act,
                "target_type":  ttype,
                "target_ref":   tref,
                "project_id":   pid,
                "ip":           ip,
                "user_agent":   ua,
                "details":      details,        # 已是 dict（psycopg3 自動 decode jsonb）
                "payload_hash": ph,
                "status_code":  sc,
                "error_text":   err,
            })

    return {
        "page":      page,
        "page_size": page_size,
        "total":     total,
        "items":     items,
    }


@router.get(
    "/audit/actions",
    dependencies=[Depends(get_current_user)],
)
def list_audit_actions() -> Dict[str, Any]:
    """
    取得目前 DB 內出現過的所有 action 種類，前端做下拉選單用。
    所有登入使用者都能查（純元資料，無敏感內容）。
    """
    sql = "SELECT action, COUNT(*) FROM audit_logs GROUP BY action ORDER BY COUNT(*) DESC"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, prepare=False)
        rows = cur.fetchall()
    return {"items": [{"action": r[0], "count": int(r[1])} for r in rows]}


# ============================================================
# Evidence Files 查詢（P2 證物指紋）
# ============================================================
@router.get(
    "/projects/{project_id}/evidence-files",
    dependencies=[Depends(get_current_user)],
)
def list_evidence_files(
    project_id: str,
    target_id: Optional[str] = None,
    page:      int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """
    列出 project / target 的證物檔案清單（含全 SHA-256）。
    供「證物清單」與「事後比對」使用。
    """
    where: List[str] = ["project_id = %s"]
    params: List[Any] = [project_id]
    if target_id:
        where.append("target_id = %s"); params.append(target_id)
    where_sql = "WHERE " + " AND ".join(where)

    count_sql = f"SELECT COUNT(*) FROM evidence_files {where_sql}"
    list_sql = f"""
    SELECT id, project_id, target_id, filename, ext, size_bytes,
           sha256_full, mime_hint,
           uploaded_by, uploaded_by_name, uploaded_at,
           rows_total, rows_inserted, rows_skipped
      FROM evidence_files
      {where_sql}
     ORDER BY uploaded_at DESC, id DESC
     LIMIT %s OFFSET %s
    """
    offset = (page - 1) * page_size
    items: List[Dict[str, Any]] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(count_sql, params, prepare=False)
        total = int(cur.fetchone()[0])

        cur.execute(list_sql, [*params, page_size, offset], prepare=False)
        for r in cur.fetchall():
            items.append({
                "id": r[0],
                "project_id": r[1],
                "target_id": r[2],
                "filename": r[3],
                "ext": r[4],
                "size_bytes": r[5],
                "sha256_full": r[6],
                "mime_hint": r[7],
                "uploaded_by": r[8],
                "uploaded_by_name": r[9],
                "uploaded_at": r[10].isoformat() if r[10] else None,
                "rows_total": r[11],
                "rows_inserted": r[12],
                "rows_skipped": r[13],
            })

    return {"page": page, "page_size": page_size, "total": total, "items": items}
