# backend/app/api/preview.py
"""
Preview evidence API（P9A A.3；P9 Phase 2A.3 錯誤契約重構）。

登入版 preview：POST 建立加密 artifact 並回 features（不回 _records）；GET 重解析
唯讀渲染；seal/save/delete 生命週期。save 走 server 端重解析（權威來源）+ register_evidence
+ chunked ingest，補 save-records 的證據鏈缺口。

Phase 2A.3：
  - 錯誤一律 raise AppError（core.errors），由 core.error_handlers 轉成統一 contract
    { "error": {code,message,details}, "request_id" }；controller 不再散落中文 detail。
  - endpoint 掛 response_model（schemas.preview）+ OpenAPI error responses。
  - 關鍵路徑寫結構化 log（core.logging_utils），preview_id 遮罩、不記 token / raw bytes。

A.3 範圍鎖定：只做登入版（無 guest）；不收 mapping（手動對應續走舊路徑）。
狀態碼：404 not_found / 410 expired|revoked|consumed / 403 forbidden / 409 sha 不符 /
        413 過大|中大檔 / 503 缺金鑰 / 422 診斷|加密檔|解析失敗 / 500 重建失敗。
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.security import (
    get_current_user,
    assert_project_access,
    add_project_member,
    project_has_members,
    AUTH_ENABLED,
)
from app.services import preview_artifact as pa
from app.services.preview_artifact import PreviewTooLargeError, PreviewStorageUnavailable
from app.services.crypto_box import PreviewKeyError
from app.services.ingest import parse_file_only, ingest_auto, ParseDiagnosisError, EncryptedFileError
from app.services.evidence import register_evidence, update_evidence_stats
from app.services.audit import write_audit

from app.core.errors import AppError, ErrorCode
from app.core import logging_utils as log
from app.schemas.preview import (
    ErrorResponse,
    PreviewCreateResponse,
    PreviewReadResponse,
    PreviewSealResponse,
    PreviewSaveRequest,
    PreviewSaveResponse,
    PreviewDeleteResponse,
)

router = APIRouter()

# 410 三態 code + 使用者訊息（machine-readable code 交前端判斷；中文只給人看）。
_STATE_410 = {
    "expired":  (ErrorCode.PREVIEW_EXPIRED,  "預覽已過期，請重新上傳檔案。"),
    "revoked":  (ErrorCode.PREVIEW_REVOKED,  "預覽已撤銷。"),
    "consumed": (ErrorCode.PREVIEW_CONSUMED, "預覽已完成存證，不可重複使用。"),
}
_TOO_LARGE_MSG = "檔案超過目前預覽容量，請改用正式上傳。"

# OpenAPI：讓 error contract 出現在文件（附幾個代表性狀態）。
_ERR_RESPONSES = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    410: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


# ── helpers ─────────────────────────────────────────────────
def _require_preview_owner(meta: Dict[str, Any], user: Dict[str, Any]) -> None:
    """owner（created_by）或 system admin 才可讀/seal/delete；否則 AppError 403。"""
    if user.get("role") == "admin":
        return
    cb = meta.get("created_by")
    if cb is not None and cb == user.get("id"):
        return
    raise AppError(
        code=ErrorCode.PREVIEW_FORBIDDEN,
        message="你沒有權限存取這筆預覽資料。",
        status_code=403,
    )


def _load_active(preview_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """取 meta；不存在→404、非 owner/admin→403、非 active→410。回 meta。"""
    meta = pa.get_meta(preview_id)
    if meta is None:
        raise AppError(
            code=ErrorCode.PREVIEW_NOT_FOUND,
            message="找不到這筆預覽資料。",
            status_code=404,
        )
    _require_preview_owner(meta, user)       # 先驗身分（私有資源），再驗狀態
    st = pa.state_of(meta)
    if st != "active":
        code, msg = _STATE_410.get(st, (ErrorCode.PREVIEW_REVOKED, "預覽已失效。"))
        raise AppError(code=code, message=msg, status_code=410)
    return meta


def _records_to_features(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """與 parse_only/parse-temp 相同：lat/lng 非空才進 features；回 (features, skipped)。"""
    features: List[Dict[str, Any]] = []
    skipped = 0
    for r in records:
        if r.get("lat") is None or r.get("lng") is None:
            skipped += 1
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": {
                "target_id":   r.get("target_id"),
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
    return features, skipped


# ── POST /api/preview ───────────────────────────────────────
@router.post("/preview", response_model=PreviewCreateResponse, responses=_ERR_RESPONSES)
@router.post("/preview/", response_model=PreviewCreateResponse, include_in_schema=False)
async def create_preview(
    request: Request,
    file: UploadFile = File(...),
    target_id: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    filename = file.filename or "upload"
    content = await file.read()

    # size guard（在解析之前，省掉大檔白解析）
    try:
        kind = pa.choose_storage_kind(len(content))
    except PreviewTooLargeError:
        raise AppError(code=ErrorCode.PREVIEW_TOO_LARGE, message=_TOO_LARGE_MSG, status_code=413)
    if kind == "object":
        # A.3 object 分支為 stub → 中大檔一律引導走正式 /upload（413）
        raise AppError(code=ErrorCode.PREVIEW_TOO_LARGE, message=_TOO_LARGE_MSG, status_code=413)

    if not target_id:
        target_id = filename.rsplit(".", 1)[0]

    try:
        records = parse_file_only(target_id, filename, content, mapping=None)
    except EncryptedFileError as e:
        raise AppError(code=ErrorCode.PREVIEW_PARSE_FAILED, message=str(e), status_code=422)
    except ParseDiagnosisError as e:
        # 使用者檔案格式問題 → 422；diagnosis 放非敏感 details 供前端手動對應。
        raise AppError(
            code=ErrorCode.PREVIEW_PARSE_FAILED,
            message="無法自動辨識此檔案格式。",
            status_code=422,
            details={"diagnosis": e.diagnosis, "filename": filename},
        )
    except ValueError as e:
        raise AppError(code=ErrorCode.PREVIEW_PARSE_FAILED, message=str(e), status_code=422)

    features, skipped = _records_to_features(records)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
    created_by = current_user["id"] if current_user.get("id") else None

    try:
        art = pa.create(
            raw=content, records=records, filename=filename, ext=ext,
            parser_type="auto",
            provenance={"pipeline_version": "P9", "target_id": target_id},
            created_by=created_by,
        )
    except PreviewKeyError:
        # 伺服器設定問題（金鑰缺失）→ 503（fail-closed，絕不明文儲存）
        raise AppError(
            code=ErrorCode.PREVIEW_KEY_MISSING,
            message="預覽加密服務尚未完成設定，請聯絡系統管理員。",
            status_code=503,
        )
    except (PreviewTooLargeError, PreviewStorageUnavailable):
        raise AppError(code=ErrorCode.PREVIEW_TOO_LARGE, message=_TOO_LARGE_MSG, status_code=413)

    write_audit(
        action="preview.create", user=current_user, request=request,
        target_type="preview", target_ref=art["preview_id"],
        details={
            "sha256_full": art["sha256_full"], "parser_type": "auto",
            "size_bytes": art["size_bytes"], "storage_kind": art["storage_kind"],
            "row_count": art["row_count"], "plotted": len(features), "skipped": skipped,
        },
        status_code=200,
    )
    log.log_info(
        "preview.create.ok",
        preview_id_masked=log.mask_preview_id(art["preview_id"]),
        user_id=current_user.get("id"),
        row_count=art["row_count"], plotted=len(features), skipped=skipped,
        storage_kind=art["storage_kind"], status_code=200,
    )
    return {
        "preview_id": art["preview_id"],
        "features": features,
        "total": len(records),
        "plotted": len(features),
        "skipped": skipped,
        "parser_type": "auto",
        "expires_at": art["expires_at"].isoformat(),
    }


# ── GET /api/preview/{id} ───────────────────────────────────
@router.get("/preview/{preview_id}", response_model=PreviewReadResponse, responses=_ERR_RESPONSES)
def read_preview(
    preview_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    meta = _load_active(preview_id, current_user)   # 404/403/410
    raw = pa.load_raw(preview_id)                    # 解密（db 分支）
    target_id = (meta.get("provenance") or {}).get("target_id") or meta["filename"].rsplit(".", 1)[0]
    try:
        records = parse_file_only(target_id, meta["filename"], raw, mapping=None)
    except Exception as e:
        # artifact 已建立但 server 重建失敗 → 500（非使用者檔案問題）。
        # 安全：不得把底層 exception message（str(e)）寫進 audit / log —— 它可能含檔案路徑、
        # SQL、連線字串、原始資料或 secret。audit error_text 只留「固定安全摘要 + exception
        # class 名稱」；structured log 只留 error_type / error_stage（皆不含 str(e)）。
        write_audit(
            action="preview.read", user=current_user, request=request,
            target_type="preview", target_ref=preview_id,
            status_code=500, error_text=f"preview rebuild failed: {type(e).__name__}",
        )
        # domain event：記錄「非 AppError」底層根因的 class 名稱——全域 AppError handler
        # 看不到這個被吞掉的原始 exception。全域 handler 另會 emit app.error.server（契約層），
        # 兩者資訊互補、非重複。
        log.log_error(
            "preview.read.rebuild_failed",
            preview_id_masked=log.mask_preview_id(preview_id),
            error_type=type(e).__name__, error_stage="preview_rebuild", status_code=500,
        )
        raise AppError(
            code=ErrorCode.PREVIEW_PARSE_FAILED,
            message="預覽重建失敗，請重新上傳。",
            status_code=500,
        )

    features, skipped = _records_to_features(records)
    # pure read on artifact：不 UPDATE preview_artifacts、只寫 audit + log
    write_audit(
        action="preview.read", user=current_user, request=request,
        target_type="preview", target_ref=preview_id, status_code=200,
    )
    log.log_info(
        "preview.read.ok",
        preview_id_masked=log.mask_preview_id(preview_id),
        user_id=current_user.get("id"), plotted=len(features), skipped=skipped, status_code=200,
    )
    return {
        "features": features,
        "total": len(records),
        "plotted": len(features),
        "skipped": skipped,
    }


# ── POST /api/preview/{id}/seal ─────────────────────────────
@router.post("/preview/{preview_id}/seal", response_model=PreviewSealResponse, responses=_ERR_RESPONSES)
def seal_preview(
    preview_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    _load_active(preview_id, current_user)           # 404/403/410
    pa.analyst_seal(preview_id, current_user.get("id"))
    write_audit(
        action="preview.seal", user=current_user, request=request,
        target_type="preview", target_ref=preview_id, status_code=200,
    )
    log.log_info(
        "preview.seal.ok",
        preview_id_masked=log.mask_preview_id(preview_id),
        user_id=current_user.get("id"), status_code=200,
    )
    return {"ok": True}


# ── POST /api/preview/{id}/save ─────────────────────────────
@router.post("/preview/{preview_id}/save", response_model=PreviewSaveResponse, responses=_ERR_RESPONSES)
def save_preview(
    preview_id: str,
    body: PreviewSaveRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    t0 = perf_counter()
    meta = _load_active(preview_id, current_user)    # 404/403/410（owner/admin）
    project_id = body.project_id.strip()
    if not project_id:
        raise AppError(
            code=ErrorCode.VALIDATION_ERROR,
            message="project_id 必填",
            status_code=400,
        )
    target_id = body.target_id.strip() or meta["filename"].rsplit(".", 1)[0]

    # 目標 project 權限（沿用 upload.py：新 project 自動 owner；否則 collaborator+）
    # assert_project_access 失敗會 raise HTTPException(403) → preview-scoped handler 轉 PREVIEW_FORBIDDEN。
    if AUTH_ENABLED and current_user.get("role") != "admin":
        if not project_has_members(project_id):
            add_project_member(project_id, current_user["id"], "owner", granted_by=current_user["id"])
        else:
            assert_project_access(current_user, project_id, "collaborator")

    # 完整性 gate：raw 再 hash 必須等於建立時的 sha256_full（deterministic）
    raw = pa.load_raw(preview_id)
    if pa.sha256_hex(raw) != meta["sha256_full"]:
        # 契約層 log 由全域 AppError handler 統一 emit（app.error.client / 409 /
        # PREVIEW_SHA_MISMATCH / request_id）；此處不再重複 log，避免同一錯誤兩筆 structured log。
        raise AppError(
            code=ErrorCode.PREVIEW_SHA_MISMATCH,
            message="原始檔完整性驗證失敗，請重新建立預覽。",
            status_code=409,
        )

    # inline analyst seal（若尚未 seal）
    if not meta.get("sealed_at"):
        if pa.analyst_seal(preview_id, current_user.get("id")):
            write_audit(
                action="preview.seal", user=current_user, request=request,
                target_type="preview", target_ref=preview_id, status_code=200,
            )

    # 證據鏈 + 落地（server 端重解析 + chunked ingest；等同 /upload (A)+(B)）
    ev = register_evidence(
        project_id=project_id, target_id=target_id,
        filename=meta["filename"], ext=meta.get("ext"), content=raw,
        uploaded_by=current_user.get("id"), uploaded_by_name=current_user.get("username"),
    )
    result = ingest_auto(project_id, target_id, meta["filename"], raw)
    total = int(result.get("total") or 0)
    inserted = int(result.get("inserted") or 0)
    skipped = int(result.get("skipped") or 0)
    update_evidence_stats(ev["id"], total, inserted, skipped)

    pa.mark_consumed(preview_id, project_id, target_id)
    write_audit(
        action="preview.consume", user=current_user, request=request,
        target_type="raw_traces", target_ref=target_id, project_id=project_id,
        details={
            "preview_id": preview_id, "evidence_id": ev["id"],
            "sha256_full": meta["sha256_full"],
            "total": total, "inserted": inserted, "skipped": skipped,
        },
        status_code=200,
    )
    log.log_info(
        "preview.save.ok",
        preview_id_masked=log.mask_preview_id(preview_id),
        user_id=current_user.get("id"), project_id=project_id, target_id=target_id,
        evidence_id=ev["id"], total=total, inserted=inserted, skipped=skipped,
        duration_ms=round((perf_counter() - t0) * 1000, 1), status_code=200,
    )
    return {
        "ok": True,
        "evidence_id": ev["id"],
        "sha256_full": meta["sha256_full"],
        "total": total,
        "inserted": inserted,
        "skipped": skipped,
    }


# ── DELETE /api/preview/{id} ────────────────────────────────
@router.delete("/preview/{preview_id}", response_model=PreviewDeleteResponse, responses=_ERR_RESPONSES)
def delete_preview(
    preview_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    _load_active(preview_id, current_user)           # 404/403/410
    pa.revoke(preview_id)
    write_audit(
        action="preview.delete", user=current_user, request=request,
        target_type="preview", target_ref=preview_id, status_code=200,
    )
    log.log_info(
        "preview.delete.ok",
        preview_id_masked=log.mask_preview_id(preview_id),
        user_id=current_user.get("id"), status_code=200,
    )
    return {"ok": True}
