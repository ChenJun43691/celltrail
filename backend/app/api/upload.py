from fastapi import APIRouter, UploadFile, File, Form
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.services.ingest import ingest_csv

router = APIRouter()

@router.post("/")
async def upload_file(
    project_id: str = Form(...),
    target_id: str = Form(...),
    file: UploadFile = File(...)
):
    if not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(status_code=400, detail="只接受 CSV/TXT 檔案")
    content = await file.read()
    result = ingest_csv(project_id, target_id, content)
    return {"project_id": project_id, "target_id": target_id, "filename": file.filename, **result}

router = APIRouter()

@router.post("/")
async def upload_file(
    project_id: str = Form(...),
    target_id: str = Form(...),
    file: UploadFile = File(...)
):
    # 先做最小驗證：讀前幾行，回回顧資訊
    head = (await file.read(2048)).decode(errors="ignore")
    lines = head.splitlines()[:5]
    return {
        "project_id": project_id,
        "target_id": target_id,
        "filename": file.filename,
        "preview": lines
    }