# backend/app/api/share.py
"""
專案分享連結（12 小時臨時免登入檢視）
=========================================================
端點：
  POST   /api/projects/{project_id}/share-links   建立分享連結（owner / admin）
  GET    /api/projects/{project_id}/share-links   列出此專案的分享連結（owner / admin）
  DELETE /api/share-links/{token}                 撤銷分享連結（owner / admin）
  GET    /api/share/{token}                       公開：免登入取得專案地圖（純檢視）

安全模型（詳見 migration_share_links.sql 開頭）：
  - 「任何人持連結即可免登入檢視」；唯一防線是 token 不可猜測（192-bit 熵）。
  - 純檢視：公開端點只回傳地圖 GeoJSON，不提供任何寫入或下載報告的入口。
  - 每次開啟連結都會寫一筆 audit_logs（action=share_link.view，含 IP），
    管理者可在 audit.html 追蹤。
"""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db.session import get_conn
from app.security import get_current_user
from app.services.audit import write_audit

router = APIRouter(tags=["share"])

# 連結效期：固定 12 小時，不開放呼叫端自訂（避免有人開出永久連結）。
SHARE_LINK_TTL = timedelta(hours=12)


# ---------- 輔助 ----------
def _require_project_owner(project_id: str, user: dict) -> None:
    """只有專案 owner 或系統 admin 能建立／撤銷分享連結。"""
    if user.get("role") == "admin":
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT permission FROM project_members
             WHERE project_id = %s AND user_id = %s
               AND (expires_at IS NULL OR expires_at > now())
            """,
            (project_id, user["id"]), prepare=False,
        )
        row = cur.fetchone()
    if not row or row[0] != "owner":
        raise HTTPException(status_code=403, detail="需要此案件的 owner 或 admin 身份")


def _fetch_map_geojson(project_id: str, limit: int = 10000) -> dict:
    """
    取地圖 GeoJSON：與 /api/projects/{id}/map-layers 完全相同的查詢規則
    —— 只回「已定位（geom IS NOT NULL）」且「未軟刪（deleted_at IS NULL）」的列。
    為保持 share.py 自足、不牽動 map.py，此處刻意複製查詢而非 import。
    """
    sql = """
    WITH rows AS (
      SELECT target_id, start_ts, end_ts, cell_id, cell_addr,
             sector_name, site_code, sector_id, azimuth, azimuth_ref,
             accuracy_m, geom
        FROM raw_traces
       WHERE project_id = %s AND geom IS NOT NULL AND deleted_at IS NULL
       ORDER BY start_ts NULLS LAST, id
       LIMIT %s
    )
    SELECT jsonb_build_object(
      'type', 'FeatureCollection',
      'features', COALESCE(jsonb_agg(jsonb_build_object(
          'type', 'Feature',
          'geometry', ST_AsGeoJSON(geom)::jsonb,
          'properties', jsonb_strip_nulls(jsonb_build_object(
              'target_id',   target_id,   'start_ts',    start_ts,
              'end_ts',      end_ts,      'cell_id',     cell_id,
              'cell_addr',   cell_addr,   'sector_name', sector_name,
              'site_code',   site_code,   'sector_id',   sector_id,
              'azimuth',     azimuth,     'azimuth_ref', azimuth_ref,
              'accuracy_m',  accuracy_m))
      )), '[]'::jsonb)
    ) FROM rows
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id, limit), prepare=False)
        r = cur.fetchone()
    return r[0] if r and r[0] else {"type": "FeatureCollection", "features": []}


# ---------- POST 建立分享連結 ----------
@router.post("/projects/{project_id}/share-links")
def create_share_link(
    project_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """建立一條 12 小時分享連結。需 owner 或 admin。"""
    _require_project_owner(project_id, current_user)

    # 為不存在資料的 project 開連結沒有意義 → 先確認此 project 有未刪除的列。
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM raw_traces WHERE project_id=%s AND deleted_at IS NULL LIMIT 1",
            (project_id,), prepare=False,
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="此專案尚無資料，無法建立分享連結")

    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + SHARE_LINK_TTL
    # id=0 = 匿名 admin（AUTH_ENABLED=false），不在 users 表，FK 必須用 NULL。
    creator_id = current_user["id"] if current_user.get("id") != 0 else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO share_links (token, project_id, created_by, expires_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id, token, created_at, expires_at
            """,
            (token, project_id, creator_id, expires_at), prepare=False,
        )
        row = cur.fetchone()

    write_audit(
        action="share_link.create", user=current_user,
        target_type="share_link", target_ref=token, project_id=project_id,
        request=request,
        details={"share_link_id": int(row[0]), "expires_at": row[3].isoformat()},
    )
    return {
        "ok": True,
        "token": row[1],
        "project_id": project_id,
        "created_at": row[2].isoformat(),
        "expires_at": row[3].isoformat(),
    }


# ---------- GET 列出此專案的分享連結 ----------
@router.get("/projects/{project_id}/share-links")
def list_share_links(
    project_id: str,
    current_user: dict = Depends(get_current_user),
):
    """列出此專案所有分享連結（含已過期／已撤銷，供 owner 檢視）。需 owner 或 admin。"""
    _require_project_owner(project_id, current_user)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.token, s.created_at, s.expires_at, s.revoked_at,
                   s.last_used_at, s.use_count, u.username
              FROM share_links s
              LEFT JOIN users u ON u.id = s.created_by
             WHERE s.project_id = %s
             ORDER BY s.created_at DESC
            """,
            (project_id,), prepare=False,
        )
        rows = cur.fetchall()

    now = datetime.now(timezone.utc)
    items = [
        {
            "token":        r[0],
            "created_at":   r[1].isoformat(),
            "expires_at":   r[2].isoformat(),
            "revoked_at":   r[3].isoformat() if r[3] else None,
            "last_used_at": r[4].isoformat() if r[4] else None,
            "use_count":    int(r[5]),
            "created_by":   r[6],
            "is_valid":     (r[3] is None) and (r[2] > now),
        }
        for r in rows
    ]
    return {"project_id": project_id, "total": len(items), "items": items}


# ---------- DELETE 撤銷分享連結 ----------
@router.delete("/share-links/{token}")
def revoke_share_link(
    token: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """撤銷一條分享連結（設 revoked_at）。需該專案 owner 或 admin。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, revoked_at FROM share_links WHERE token=%s",
            (token,), prepare=False,
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="找不到此分享連結")

    project_id, revoked_at = row
    _require_project_owner(project_id, current_user)
    if revoked_at is not None:
        return {"ok": True, "project_id": project_id, "token": token, "already_revoked": True}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE share_links SET revoked_at = now() WHERE token=%s AND revoked_at IS NULL",
            (token,), prepare=False,
        )
    write_audit(
        action="share_link.revoke", user=current_user,
        target_type="share_link", target_ref=token, project_id=project_id,
        request=request,
    )
    return {"ok": True, "project_id": project_id, "token": token}


# ---------- GET 公開檢視（免登入） ----------
@router.get("/share/{token}")
def view_shared_project(token: str, request: Request):
    """
    公開端點（無 auth dependency）：憑 token 取得該專案地圖 GeoJSON（純檢視）。
      - token 不存在            → 404
      - token 已撤銷 / 已過期    → 410 Gone
    每次成功檢視都會 use_count+1、更新 last_used_at，並寫一筆 audit。
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, expires_at, revoked_at FROM share_links WHERE token=%s",
            (token,), prepare=False,
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="連結不存在")

    project_id, expires_at, revoked_at = row
    if revoked_at is not None:
        raise HTTPException(status_code=410, detail="此分享連結已被撤銷")
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="此分享連結已過期（連結有效期為 12 小時）")

    geojson = _fetch_map_geojson(project_id)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE share_links SET use_count = use_count + 1, last_used_at = now() WHERE token=%s",
            (token,), prepare=False,
        )
    write_audit(
        action="share_link.view", project_id=project_id,
        target_type="share_link", target_ref=token, request=request,
        details={"feature_count": len(geojson.get("features", []))},
    )
    return {
        "project_id": project_id,
        "expires_at": expires_at.isoformat(),
        "geojson": geojson,
    }
