# backend/app/api/report.py
"""
證物報告 PDF 端點（P2）
============================================================
GET /api/projects/{project_id}/evidence-report?target_id=...

權限：admin（敏感資料：含完整 audit 時間軸 + SHA-256 全字串）
回傳：application/pdf StreamingResponse
副作用：寫入 audit_logs（action='export_report'）
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import io

from app.security import get_current_user, require_admin
from app.services.audit import write_audit
from app.services.report import build_evidence_report

router = APIRouter()


@router.get(
    "/projects/{project_id}/evidence-report",
    dependencies=[Depends(require_admin)],
)
def evidence_report(
    project_id: str,
    request: Request,
    target_id: Optional[str] = Query(None, description="留空 = 全部 target"),
    current_user: dict = Depends(get_current_user),
):
    """產出 PDF 報告。下載檔名：CellTrail_{project_id}_{YYYYmmdd_HHMMSS}.pdf"""
    try:
        pdf_bytes = build_evidence_report(
            project_id=project_id,
            target_id=target_id,
            requested_by=(current_user.get("username") or "anonymous"),
        )
    except Exception as e:
        write_audit(
            action="export_report_failed",
            user=current_user, request=request,
            target_type="project", target_ref=target_id, project_id=project_id,
            details={"target_id": target_id, "exc_type": type(e).__name__},
            status_code=500, error_text=str(e),
        )
        raise HTTPException(status_code=500, detail=f"報告產出失敗：{type(e).__name__}: {e}")

    # 寫 audit
    write_audit(
        action="export_report",
        user=current_user, request=request,
        target_type="project", target_ref=target_id, project_id=project_id,
        details={
            "target_id": target_id,
            "report_size_bytes": len(pdf_bytes),
            "report_version": "v1",
        },
        status_code=200,
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"CellTrail_{project_id}_{ts}.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )
