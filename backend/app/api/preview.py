# backend/app/api/preview.py
"""
Preview evidence API（P9A A.3，2026-07-02）。

登入版 preview：POST 建立加密 artifact 並回 features（不回 _records）；GET 重解析
唯讀渲染；seal/save/delete 生命週期。save 走 server 端重解析（權威來源）+ register_evidence
+ chunked ingest，補 save-records 的證據鏈缺口。

A.3 範圍鎖定：
  - 只做登入版（無 guest；訪客續走舊 parse-only）。
  - 不收 mapping（手動對應 preview 續走舊路徑）。
  - 不動 frontend（前端切換歸 Phase 2）。
狀態碼：404 not_found / 410 expired|revoked|consumed / 403 forbidden / 409 sha256 不符 /
        413 過大|中大檔(object stub) / 503 缺金鑰 / 422 診斷|加密檔。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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

router = APIRouter()

_TOO_LARGE_MSG = "preview 暫不支援中大檔，請改用正式 /upload"
_STATE_410 = {
    "expired": "preview 已過期",
    "revoked": "preview 已撤銷",
    "consumed": "preview 已存證，不可重複使用",
}


class SavePreviewIn(BaseModel):
    project_id: str
    target_id: str = ""


# ── helpers ─────────────────────────────────────────────────
def _require_preview_owner(meta: Dict[str, Any], user: Dict[str, Any]) -> None:
    """owner（created_by）或 system admin 才可讀/seal/delete；否則 403。"""
    if user.get("role") == "admin":
        return
    cb = meta.get("created_by")
    if cb is not None and cb == user.get("id"):
        return
    raise HTTPException(status_code=403, detail="無此 preview 的存取權限")


def _load_active(preview_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """取 meta；不存在→404、非 owner/admin→403、非 active→410。回 meta。"""
    meta = pa.get_meta(preview_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="preview 不存在")
    _require_preview_owner(meta, user)       # 先驗身分（私有資源），再驗狀態
    st = pa.state_of(meta)
    if st != "active":
        raise HTTPException(status_code=410, detail=_STATE_410.get(st, "preview 已失效"))
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
@router.post("/preview")
@router.post("/preview/")
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
        raise HTTPException(status_code=413, detail=_TOO_LARGE_MSG)
    if kind == "object":
        # A.3 object 分支為 stub → 中大檔一律引導走正式 /upload（413，非 503）
        raise HTTPException(status_code=413, detail=_TOO_LARGE_MSG)

    if not target_id:
        target_id = filename.rsplit(".", 1)[0]

    try:
        records = parse_file_only(target_id, filename, content, mapping=None)
    except EncryptedFileError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ParseDiagnosisError as e:
        return JSONResponse(status_code=422, content={
            "ok": False, "error": "format_unknown", "detail": str(e),
            "diagnosis": e.diagnosis, "filename": filename,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
        raise HTTPException(status_code=503, detail="preview 加密金鑰未設定，暫時無法建立 preview")
    except PreviewTooLargeError:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_MSG)
    except PreviewStorageUnavailable:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_MSG)

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
@router.get("/preview/{preview_id}")
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
        write_audit(
            action="preview.read", user=current_user, request=request,
            target_type="preview", target_ref=preview_id,
            status_code=500, error_text=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(status_code=500, detail="preview 重建失敗")

    features, skipped = _records_to_features(records)
    # pure read on artifact：不 UPDATE preview_artifacts、不做 system_seal_verify；只寫 audit
    write_audit(
        action="preview.read", user=current_user, request=request,
        target_type="preview", target_ref=preview_id, status_code=200,
    )
    return {
        "features": features,
        "total": len(records),
        "plotted": len(features),
        "skipped": skipped,
    }


# ── POST /api/preview/{id}/seal ─────────────────────────────
@router.post("/preview/{preview_id}/seal")
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
    return {"ok": True}


# ── POST /api/preview/{id}/save ─────────────────────────────
@router.post("/preview/{preview_id}/save")
def save_preview(
    preview_id: str,
    body: SavePreviewIn,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    meta = _load_active(preview_id, current_user)    # 404/403/410（owner/admin）
    project_id = body.project_id.strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id 必填")
    target_id = body.target_id.strip() or meta["filename"].rsplit(".", 1)[0]

    # 目標 project 權限（沿用 upload.py：新 project 自動 owner；否則 collaborator+）
    if AUTH_ENABLED and current_user.get("role") != "admin":
        if not project_has_members(project_id):
            add_project_member(project_id, current_user["id"], "owner", granted_by=current_user["id"])
        else:
            assert_project_access(current_user, project_id, "collaborator")

    # 完整性 gate：raw 再 hash 必須等於建立時的 sha256_full（deterministic）
    raw = pa.load_raw(preview_id)
    if pa.sha256_hex(raw) != meta["sha256_full"]:
        raise HTTPException(status_code=409, detail="檔案指紋不符，拒絕存證")

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
    return {
        "ok": True,
        "evidence_id": ev["id"],
        "sha256_full": meta["sha256_full"],
        "total": total,
        "inserted": inserted,
        "skipped": skipped,
    }


# ── DELETE /api/preview/{id} ────────────────────────────────
@router.delete("/preview/{preview_id}")
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
    return {"ok": True}
