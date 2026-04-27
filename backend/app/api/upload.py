# backend/app/api/upload.py
"""
檔案上傳端點：CSV / TSV / TXT / XLSX(系列) / PDF。

P0+P2 改動（2026-04-26）：
  - 上傳前：register_evidence() 把整個 raw bytes 的 SHA-256 + 大小 落地至 evidence_files
  - 上傳成功 → write_audit(action='upload', evidence_id=..., sha256_full=...)
  - 上傳失敗 → write_audit(action='upload_failed', ...)
  - audit_logs 內含 evidence_id，可雙向 join：證物 ↔ 操作紀錄
"""
import traceback

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, Request

from app.security import get_current_user
from app.services.audit import write_audit
from app.services.evidence import register_evidence, update_evidence_stats
from app.services.ingest import ingest_auto, ingest_pdf

router = APIRouter()


# 同時支援 /api/upload 及 /api/upload/
@router.post("")   # /api/upload
@router.post("/")  # /api/upload/
async def upload_file(
    request: Request,
    project_id: str = Form(...),
    target_id: str = Form(""),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    filename = file.filename or ""
    content = await file.read()
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    mime = file.content_type

    # target_id 留空就以檔名(去副檔名)
    if not target_id:
        target_id = filename.rsplit(".", 1)[0]

    print(
        f"[upload] from={request.client.host if request.client else '?'} "
        f"user={current_user.get('username')} project={project_id} "
        f"target={target_id} name={filename} size={len(content)}B"
    )

    # ---- (A) 證物指紋封存（在 ingest 之前；若 ingest 失敗 hash 仍在）----
    evidence: dict | None = None
    try:
        evidence = register_evidence(
            project_id=project_id,
            target_id=target_id,
            filename=filename,
            ext=ext,
            content=content,
            mime_hint=mime,
            uploaded_by=current_user.get("id"),
            uploaded_by_name=current_user.get("username"),
        )
        print(f"[upload] evidence_id={evidence['id']} sha256={evidence['sha256_full'][:16]}... "
              f"prior_uploads={evidence['prior_uploads']}")
    except Exception as e:
        # 指紋封存失敗 → 視為嚴重錯誤，不繼續 ingest（避免「進得去但無 hash」的證物）
        traceback.print_exc()
        write_audit(
            action="upload_failed",
            user=current_user, request=request,
            target_type="raw_traces", target_ref=target_id, project_id=project_id,
            details={"filename": filename, "stage": "evidence_register",
                     "exc_type": type(e).__name__},
            status_code=500, error_text=str(e),
        )
        raise HTTPException(status_code=500, detail=f"證物指紋封存失敗：{type(e).__name__}: {e}")

    # ---- (B) 解析 + 入庫 ----
    try:
        if ext == "pdf":
            result = ingest_pdf(project_id, target_id, content)
        else:
            result = ingest_auto(project_id, target_id, filename, content)

        # 回填 evidence 統計
        update_evidence_stats(
            evidence_id=evidence["id"],
            rows_total=int(result.get("total") or 0),
            rows_inserted=int(result.get("inserted") or 0),
            rows_skipped=int(result.get("skipped") or 0),
        )

        # ---- 成功：寫入 audit ----
        write_audit(
            action="upload",
            user=current_user,
            target_type="raw_traces",
            target_ref=target_id,
            project_id=project_id,
            request=request,
            details={
                "filename":      filename,
                "ext":           ext,
                "size_bytes":    evidence["size_bytes"],
                "evidence_id":   evidence["id"],
                "sha256_full":   evidence["sha256_full"],
                "prior_uploads": evidence["prior_uploads"],
                "mime_hint":     mime,
                "total":    result.get("total"),
                "inserted": result.get("inserted"),
                "skipped":  result.get("skipped"),
                "errors_n": len(result.get("errors") or []),
            },
            status_code=200,
        )
        return {
            "ok": True,
            "filename": filename,
            "project_id": project_id,
            "target_id": target_id,
            "evidence_id":  evidence["id"],
            "sha256_full":  evidence["sha256_full"],
            "prior_uploads": evidence["prior_uploads"],
            **(result or {}),
        }

    except HTTPException as he:
        write_audit(
            action="upload_failed",
            user=current_user, request=request,
            target_type="raw_traces", target_ref=target_id, project_id=project_id,
            details={
                "filename": filename, "stage": "ingest",
                "evidence_id": evidence["id"],
                "sha256_full": evidence["sha256_full"],
                "exc_type": "HTTPException",
            },
            status_code=he.status_code, error_text=str(he.detail),
        )
        raise

    except Exception as e:
        traceback.print_exc()
        write_audit(
            action="upload_failed",
            user=current_user, request=request,
            target_type="raw_traces", target_ref=target_id, project_id=project_id,
            details={
                "filename": filename, "stage": "ingest",
                "evidence_id": evidence["id"],
                "sha256_full": evidence["sha256_full"],
                "exc_type": type(e).__name__,
            },
            status_code=400, error_text=str(e),
        )
        raise HTTPException(status_code=400, detail=f"匯入失敗：{type(e).__name__}: {e}")
