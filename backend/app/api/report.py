# backend/app/api/report.py
"""
證物報告 PDF 端點（P2）
============================================================
GET /api/projects/{project_id}/evidence-report?target_id=...

權限：**該專案的 viewer 以上**（2026-08-25 決策，見下）。

  原本實作是 `require_admin` + `assert_project_access(viewer)` 兩者都要 ——
  於是專案 owner 自己也下載不了自己案件的報告，只有系統 admin 能出。
  而同一支函式的 docstring 一直寫著「需 viewer 以上」，規格與實作矛盾了
  一個多月（P9 Phase 2A 列為 REPORT_ACL_SPEC_MISMATCH，因屬產品決策而未逕行修改）。

  改採 viewer+ 的理由：偵查實務上承辦人要為自己的案件出報告，
  每份都得找系統管理員代勞並不合理。存取邊界仍在 —— `assert_project_access`
  會擋掉所有非該專案成員；而報告內容（audit 時間軸、SHA-256）本就是該案成員
  在系統內看得到的資料，不因匯出成 PDF 而變得更敏感。
  每次下載仍寫 `audit_logs`（action='export_report'），誰在何時匯出留有紀錄。
回傳：application/pdf StreamingResponse
副作用：寫入 audit_logs（action='export_report'）
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import io

from app.security import assert_project_access, get_current_user
from app.services.audit import write_audit
from app.services.report import build_evidence_report

router = APIRouter()


@router.get(
    "/projects/{project_id}/evidence-report",
)
def evidence_report(
    project_id: str,
    request: Request,
    target_id: Optional[str] = Query(None, description="留空 = 全部 target"),
    current_user: dict = Depends(get_current_user),
):
    """產出 PDF 報告。下載檔名：CellTrail_{project_id}_{YYYYmmdd_HHMMSS}.pdf

    權限：該專案的 viewer 以上（system admin 由 assert_project_access 內部放行）。
    """
    assert_project_access(current_user, project_id, "viewer")
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
