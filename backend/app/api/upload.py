# backend/app/api/upload.py
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from app.services.ingest import ingest_auto, ingest_pdf
from app.security import get_current_user
import traceback

router = APIRouter()

# 同一支函式同時處理 /api/upload 與 /api/upload/
@router.post("", tags=["upload"])
@router.post("/", tags=["upload"])
async def upload_file(
    project_id: str = Form(...),
    target_id: str = Form(""),
    file: UploadFile = File(...),
    user = Depends(get_current_user),  # ← 需要 Bearer Token
):
    try:
        filename = file.filename or ""
        content = await file.read()
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # target_id 留空就用檔名（去副檔名）
        if not target_id:
            target_id = filename.rsplit(".", 1)[0] if filename else "unknown"

        if ext == "pdf":
            result = ingest_pdf(project_id, target_id, content)
        else:
            result = ingest_auto(project_id, target_id, filename, content)

        return {
            "ok": True,
            "filename": filename,
            "project_id": project_id,
            "target_id": target_id,
            **(result or {}),
        }

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"匯入失敗：{type(e).__name__}: {e}")