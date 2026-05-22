# backend/app/api/carrier_profile.py
"""
電信業者欄名對照表管理（carrier_profiles）

端點（均需 admin）：
  GET    /api/admin/carrier-profile          查看當前 default profile（DB mapping_json + 合併後總計）
  PATCH  /api/admin/carrier-profile/entry    新增或覆蓋單一 mapping entry
  DELETE /api/admin/carrier-profile/entry    刪除單一 mapping entry（只影響 DB 自訂；code 預設不可刪）
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.db.session import get_conn
from app.security import require_admin
from app.services.audit import write_audit
from app.services.carrier_profile import (
    get_active_header_map,
    get_default_profile,
    invalidate_cache,
)

router = APIRouter(prefix="/admin/carrier-profile", tags=["carrier-profile"])


# ---------- GET / ----------
@router.get("")
def get_carrier_profile(_user: dict = Depends(require_admin)):
    """
    回傳：
      - profile     : DB default profile 的 metadata（id, carrier_name, variant_label, notes, updated_at）
      - db_mapping  : DB mapping_json 的 raw 內容（管理員自訂、可編輯／刪除的部分）
      - code_mapping: 程式碼內建預設對照（ingest._RAW2CANON，唯讀；DB 自訂同名 key 會覆蓋它）
      - active_count: 合併後生效的 mapping 總條目數
      - source      : "db" | "code_fallback"
    """
    from app.services.ingest import _RAW2CANON  # lazy import：避免 import 期 circular
    code_mapping = dict(_RAW2CANON)

    profile = get_default_profile()
    active_map = get_active_header_map()

    if profile is None:
        return {
            "profile": None,
            "db_mapping": {},
            "code_mapping": code_mapping,
            "active_count": len(active_map),
            "source": "code_fallback",
        }

    return {
        "profile": {
            "id": profile["id"],
            "carrier_name": profile["carrier_name"],
            "variant_label": profile["variant_label"],
            "notes": profile.get("notes"),
            "updated_at": profile["updated_at"].isoformat() if profile.get("updated_at") else None,
        },
        "db_mapping": profile["mapping_json"] or {},
        "code_mapping": code_mapping,
        "active_count": len(active_map),
        "source": "db",
    }


# ---------- PATCH /entry ----------
class EntryUpsert(BaseModel):
    raw_key: str
    canon_key: str


@router.patch("/entry")
def upsert_entry(body: EntryUpsert, request: Request,
                 user: dict = Depends(require_admin)):
    """
    新增或覆蓋 mapping_json 中的一個 entry。
    使用 PostgreSQL JSONB || 運算子做原子 merge，不需先讀再寫。
    """
    raw_key  = body.raw_key.strip()
    canon_key = body.canon_key.strip()
    if not raw_key or not canon_key:
        raise HTTPException(400, "raw_key 和 canon_key 均不得為空")

    patch_json = json.dumps({raw_key: canon_key})
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE carrier_profiles
                  SET mapping_json = mapping_json || %s::jsonb,
                      updated_at   = now()
                WHERE is_default = TRUE AND is_active = TRUE
            RETURNING id""",
            (patch_json,),
            prepare=False,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "找不到 default profile，請先套用 schema.sql")
        conn.commit()

    invalidate_cache()

    write_audit(
        action="upsert_carrier_profile",
        user=user, request=request,
        target_type="carrier_profile",
        details={"raw_key": raw_key, "canon_key": canon_key},
        status_code=200,
    )
    return {"ok": True, "raw_key": raw_key, "canon_key": canon_key}


# ---------- DELETE /entry ----------
class EntryDelete(BaseModel):
    raw_key: str


@router.delete("/entry")
def delete_entry(body: EntryDelete, request: Request,
                 user: dict = Depends(require_admin)):
    """
    從 mapping_json 移除指定 key。
    僅影響 DB 自訂部分；code 預設（_RAW2CANON）仍會在合併時補回，
    因此刪除的實際效果是「恢復 code 預設值」而非「完全停用此欄名」。
    """
    raw_key = body.raw_key.strip()
    if not raw_key:
        raise HTTPException(400, "raw_key 不得為空")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE carrier_profiles
                  SET mapping_json = mapping_json - %s,
                      updated_at   = now()
                WHERE is_default = TRUE AND is_active = TRUE
            RETURNING id""",
            (raw_key,),
            prepare=False,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "找不到 default profile")
        conn.commit()

    invalidate_cache()

    write_audit(
        action="delete_carrier_profile",
        user=user, request=request,
        target_type="carrier_profile",
        details={"raw_key": raw_key},
        status_code=200,
    )
    return {"ok": True, "raw_key": raw_key}
