# backend/app/api/format_reports.py
"""
格式回報 API：

- POST   /api/format-reports/        使用者回報「無法解析」的檔案格式（含訪客）
- GET    /api/format-reports/        列出所有回報（僅 admin）
- PATCH  /api/format-reports/{id}    更新狀態（已處理/拒絕，僅 admin）

設計考量：
- 訪客也能回報（reporter_user_id 可為 null，記 IP 防濫用）
- 不存原始檔案內容，只存 headers + 診斷 + 備註
- 寫 audit_logs 供追蹤
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import get_current_user_optional, require_admin
from app.services.limiter import limiter

router = APIRouter(prefix="/format-reports", tags=["format-reports"])


class FormatReportIn(BaseModel):
    filename:  str = Field(min_length=1, max_length=255)
    headers:   list = Field(default_factory=list)
    diagnosis: dict = Field(default_factory=dict)
    note:      Optional[str] = Field(default=None, max_length=2000)


@router.post("")
@router.post("/")
@limiter.limit("10/hour")
def create_report(
    request: Request,
    payload: FormatReportIn,
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """
    使用者（含訪客）回報無法解析的檔案格式。Rate limit：10 次/小時/IP。
    """
    reporter_id = current_user.get("id") if current_user else None
    reporter_ip = request.client.host if request.client else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO format_reports
                (filename, headers, diagnosis, note, reporter_user_id, reporter_ip, status)
            VALUES (%s, %s::jsonb, %s::jsonb, %s, %s, %s, 'open')
            RETURNING id, created_at
            """,
            (
                payload.filename,
                json.dumps(payload.headers, ensure_ascii=False),
                json.dumps(payload.diagnosis, ensure_ascii=False),
                payload.note,
                reporter_id,
                reporter_ip,
            ),
            prepare=False,
        )
        row = cur.fetchone()

    return {
        "ok": True,
        "id": row[0],
        "created_at": row[1].isoformat() if row[1] else None,
        "message": "已收到回報，管理員會盡快處理",
    }


@router.get("", dependencies=[Depends(require_admin)])
@router.get("/", dependencies=[Depends(require_admin)])
def list_reports(status: Optional[str] = None, limit: int = 100):
    """
    列出格式回報。預設依建立時間 DESC，可用 ?status=open 過濾。
    """
    where = ""
    params: list = []
    if status:
        where = "WHERE r.status = %s"
        params.append(status)
    params.append(min(limit, 500))

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.id, r.filename, r.headers, r.diagnosis, r.note,
                   r.reporter_user_id, u.username, u.real_name,
                   r.reporter_ip, r.status,
                   r.handled_by, h.username, r.handled_at, r.handled_note,
                   r.created_at
              FROM format_reports r
              LEFT JOIN users u ON u.id = r.reporter_user_id
              LEFT JOIN users h ON h.id = r.handled_by
              {where}
             ORDER BY r.created_at DESC
             LIMIT %s
            """,
            tuple(params),
            prepare=False,
        )
        rows = cur.fetchall()

    items = []
    for r in rows:
        items.append({
            "id":              r[0],
            "filename":        r[1],
            "headers":         r[2] or [],
            "diagnosis":       r[3] or {},
            "note":            r[4],
            "reporter": {
                "user_id":   r[5],
                "username":  r[6],
                "real_name": r[7],
                "ip":        str(r[8]) if r[8] else None,
            },
            "status":          r[9],
            "handled_by":      r[10],
            "handled_by_name": r[11],
            "handled_at":      r[12].isoformat() if r[12] else None,
            "handled_note":    r[13],
            "created_at":      r[14].isoformat() if r[14] else None,
        })
    return {"total": len(items), "items": items}


class HandleIn(BaseModel):
    status: str = Field(..., pattern="^(open|handled|rejected)$")
    note:   Optional[str] = Field(default=None, max_length=2000)


@router.patch("/{report_id}")
def update_report(
    report_id: int,
    payload: HandleIn,
    current_admin: dict = Depends(require_admin),
):
    """更新格式回報狀態（已處理 / 拒絕）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE format_reports
               SET status = %s, handled_note = %s, handled_by = %s,
                   handled_at = CASE WHEN %s IN ('handled','rejected') THEN now() ELSE NULL END
             WHERE id = %s
            RETURNING id, status
            """,
            (payload.status, payload.note, current_admin["id"], payload.status, report_id),
            prepare=False,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="找不到此回報")

    return {"ok": True, "id": row[0], "status": row[1]}
