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

from app.security import (
    add_project_member,
    assert_project_access,
    get_current_user,
    project_has_members,
    AUTH_ENABLED,
)
from app.services.audit import write_audit
from app.services.evidence import register_evidence, update_evidence_stats
import json as _json
import time as _time
from fastapi.responses import JSONResponse
from app.services.ingest import (
    ingest_auto, ingest_pdf, parse_file_only,
    ParseDiagnosisError, EncryptedFileError,
)

router = APIRouter()

# 上傳檔案大小上限：避免單一超大檔把後端記憶體吃光（OOM）。
# 真實電信歷程檔多為數 MB 級，100 MB 已非常寬鬆。
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


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
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"檔案過大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"檔案過大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
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

    # ---- 專案權限驗證 ----
    # 非 admin：需具備 collaborator 以上權限。
    # 若 project 尚無任何成員（全新 project），上傳者自動成為 owner。
    if AUTH_ENABLED and current_user.get("role") != "admin":
        if not project_has_members(project_id):
            add_project_member(project_id, current_user["id"], "owner",
                               granted_by=current_user["id"])
        else:
            assert_project_access(current_user, project_id, "collaborator")

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

    except EncryptedFileError as e:
        # 加密 / 密碼保護檔：回清楚的錯誤提醒（不嘗試解密）
        write_audit(
            action="upload_failed",
            user=current_user, request=request,
            target_type="raw_traces", target_ref=target_id, project_id=project_id,
            details={
                "filename": filename, "stage": "ingest",
                "evidence_id": evidence["id"],
                "sha256_full": evidence["sha256_full"],
                "exc_type": "EncryptedFileError",
            },
            status_code=422, error_text=str(e),
        )
        raise HTTPException(status_code=422, detail=str(e))

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


# ---------- 臨時解析端點（不寫 DB，回傳 GeoJSON）----------
@router.post("/parse-temp")
async def parse_temp(
    file: UploadFile = File(...),
    target_id: str = Form(""),
    mapping: str = Form(""),
    _current_user: dict = Depends(get_current_user),
):
    """
    解析 + geocode 但不寫 DB，供前端臨時模式使用。
    回傳 GeoJSON FeatureCollection（與 /map-layers 同格式）。

    mapping（選填）：使用者「手動欄位對應」JSON。失敗時回 422 + diagnosis。
    """
    filename = file.filename or "upload"
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"檔案過大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    _t0 = _time.perf_counter()
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"檔案過大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    print(f"[parse-temp][timing] file_read={(_time.perf_counter()-_t0)*1000:.0f}ms size={len(content)}B file={filename}")

    if not target_id:
        target_id = filename.rsplit(".", 1)[0]

    user_mapping = None
    if mapping:
        try:
            user_mapping = _json.loads(mapping)
            if not isinstance(user_mapping, dict):
                raise ValueError("mapping 必須是 JSON 物件")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"mapping JSON 格式錯誤：{e}")

    try:
        _t1 = _time.perf_counter()
        records = parse_file_only(target_id, filename, content, mapping=user_mapping)
        print(f"[parse-temp][timing] parse_total={(_time.perf_counter()-_t1)*1000:.0f}ms records={len(records)} file={filename}")
    except EncryptedFileError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ParseDiagnosisError as e:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": "format_unknown",
                "detail": str(e),
                "diagnosis": e.diagnosis,
                "filename": filename,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"解析失敗：{type(e).__name__}: {e}")

    features = []
    skipped = 0
    for r in records:
        if r.get("lat") is None or r.get("lng") is None:
            skipped += 1
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": {
                "target_id":   r["target_id"],
                "cell_addr":   r.get("cell_addr"),
                "start_ts":    r.get("start_ts"),
                "end_ts":      r.get("end_ts"),
                "cell_id":     r.get("cell_id"),
                "accuracy_m":  r.get("accuracy_m"),
                "azimuth":     r.get("azimuth"),
                "azimuth_ref": r.get("azimuth_ref", "unknown"),
                "sector_id":   r.get("sector_id"),
                "sector_name": r.get("sector_name"),
                "site_code":   r.get("site_code"),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "total":   len(records),
        "plotted": len(features),
        "skipped": skipped,
        "_source":  "parse-temp",
        "_records": records,  # 完整記錄供「儲存為專案」時使用（含無座標列）
    }
