# backend/app/api/upload.py
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, Request
from app.services.ingest import ingest_auto, ingest_pdf
from app.security import get_current_user
import traceback

router = APIRouter()

async def _do_ingest(project_id: str, target_id: str, file: UploadFile):
    filename = file.filename or ""
    content = await file.read()
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")

    # target_id 留空就以檔名(去副檔名)
    if not target_id:
        target_id = filename.rsplit(".", 1)[0]

    if ext == "pdf":
        result = ingest_pdf(project_id, target_id, content)
    else:
        result = ingest_auto(project_id, target_id, filename, content)

    return {"ok": True, "filename": filename, "project_id": project_id, "target_id": target_id, **(result or {})}

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
    try:
        # 簡單記錄，方便看 Render Logs
        print(f"[upload] from={request.client.host} user={current_user.get('username')} "
              f"project={project_id} target={target_id} name={file.filename}")
        return await _do_ingest(project_id, target_id, file)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"匯入失敗：{type(e).__name__}: {e}")