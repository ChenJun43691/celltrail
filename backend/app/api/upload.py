# app/api/upload.py
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from app.services.ingest import ingest_auto, ingest_pdf
from app.security import get_current_user
import traceback

router = APIRouter()

async def _handle_upload(
    project_id: str,
    target_id: str,
    file: UploadFile
):
    filename = file.filename or ""
    content = await file.read()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext == "pdf":
            result = ingest_pdf(project_id, target_id, content)
        else:
            result = ingest_auto(project_id, target_id, filename, content)
        return {"ok": True, "filename": filename, **result}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"匯入失敗：{type(e).__name__}: {e}")

# 帶尾斜線：/api/upload/
@router.post("/", dependencies=[Depends(get_current_user)], tags=["upload"])
async def upload_slash(
    project_id: str = Form(...),
    target_id: str = Form(...),
    file: UploadFile = File(...)
):
    return await _handle_upload(project_id, target_id, file)

# 不帶尾斜線：/api/upload
@router.post("", dependencies=[Depends(get_current_user)], include_in_schema=False)
async def upload_no_slash(
    project_id: str = Form(...),
    target_id: str = Form(...),
    file: UploadFile = File(...)
):
    return await _handle_upload(project_id, target_id, file)