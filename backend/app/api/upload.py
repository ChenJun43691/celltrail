from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.services.ingest import ingest_auto, ingest_pdf
import traceback

router = APIRouter()

@router.post("/")
async def upload_file(
    project_id: str = Form(...),
    target_id: str = Form(...),
    file: UploadFile = File(...)
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