# backend/app/api/members.py
"""
Project 成員授權管理

端點（需登入）：
  GET    /api/projects/{project_id}/members              列出成員（owner/admin）
  POST   /api/projects/{project_id}/members              授權成員（owner/admin）
  DELETE /api/projects/{project_id}/members/{user_id}   撤銷授權（owner/admin）
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import (
    assert_project_access,
    get_current_user,
    require_admin,
)

router = APIRouter(tags=["members"])


# ---------- Schemas ----------
class GrantMemberIn(BaseModel):
    user_id:    int
    permission: str = Field(pattern="^(owner|collaborator|viewer)$")
    expires_at: Optional[str] = Field(
        default=None,
        description="ISO8601 UTC 時間；留空=永久授權。例：2027-01-01T00:00:00Z",
    )


# ---------- 輔助：確認呼叫者是 owner 或 admin ----------
def _require_project_owner(project_id: str, user: dict) -> None:
    """owner 或 system admin 才能管理成員。"""
    if user["role"] == "admin":
        return
    sql = """
    SELECT permission FROM project_members
     WHERE project_id = %s AND user_id = %s
       AND (expires_at IS NULL OR expires_at > now())
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id, user["id"]), prepare=False)
        row = cur.fetchone()
    if not row or row[0] != "owner":
        raise HTTPException(status_code=403, detail="需要此案件的 owner 或 admin 身份")


# ---------- Endpoints ----------
@router.get("/projects/{project_id}/members")
def list_members(project_id: str, current_user: dict = Depends(get_current_user)):
    """查看某 project 的成員授權清單。需 owner 或 admin。"""
    _require_project_owner(project_id, current_user)

    sql = """
    SELECT pm.id, pm.user_id, u.username, u.real_name, u.unit,
           pm.permission, pm.expires_at, pm.created_at,
           gb.username AS granted_by_name
      FROM project_members pm
      JOIN users u ON u.id = pm.user_id
      LEFT JOIN users gb ON gb.id = pm.granted_by
     WHERE pm.project_id = %s
     ORDER BY pm.created_at
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id,), prepare=False)
        rows = cur.fetchall()

    items = []
    for r in rows:
        valid = r[6] is None or r[6].timestamp() > __import__("time").time()
        items.append({
            "id":            r[0],
            "user_id":       r[1],
            "username":      r[2],
            "real_name":     r[3],
            "unit":          r[4],
            "permission":    r[5],
            "expires_at":    r[6].isoformat() if r[6] else None,
            "is_valid":      valid,
            "created_at":    r[7].isoformat() if r[7] else None,
            "granted_by":    r[8],
        })

    return {"project_id": project_id, "total": len(items), "items": items}


@router.post("/projects/{project_id}/members")
def grant_member(
    project_id: str,
    payload: GrantMemberIn,
    current_user: dict = Depends(get_current_user),
):
    """
    授權成員。body: { user_id, permission, expires_at? }
    需 owner 或 admin。
    使用 ON CONFLICT DO UPDATE：重複授權 = 更新為新 permission/expires_at。
    """
    _require_project_owner(project_id, current_user)

    # 確認目標 user 存在且為活躍帳號
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, is_active FROM users WHERE id=%s",
            (payload.user_id,), prepare=False,
        )
        target = cur.fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="目標使用者不存在")
    if not target[2]:
        raise HTTPException(status_code=400, detail="目標使用者帳號已停用")

    expires_val = None
    if payload.expires_at:
        from datetime import datetime, timezone
        try:
            expires_val = datetime.fromisoformat(payload.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="expires_at 格式錯誤，請用 ISO8601")

    # id=0 = anonymous admin（AUTH_ENABLED=false），不在 users 表，FK 必須用 NULL
    granter_id = current_user["id"] if current_user["id"] != 0 else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_members (project_id, user_id, permission, expires_at, granted_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_id, user_id) DO UPDATE
                SET permission  = EXCLUDED.permission,
                    expires_at  = EXCLUDED.expires_at,
                    granted_by  = EXCLUDED.granted_by
            RETURNING id, permission, expires_at
            """,
            (project_id, payload.user_id, payload.permission,
             expires_val, granter_id),
            prepare=False,
        )
        row = cur.fetchone()

    return {
        "ok": True,
        "project_id":  project_id,
        "user_id":     payload.user_id,
        "username":    target[1],
        "permission":  row[1],
        "expires_at":  row[2].isoformat() if row[2] else None,
    }


@router.delete("/projects/{project_id}/members/{user_id}")
def revoke_member(
    project_id: str,
    user_id: int,
    current_user: dict = Depends(get_current_user),
):
    """撤銷成員授權。owner 不能撤銷自己（避免 project 失去 owner）。"""
    _require_project_owner(project_id, current_user)

    # 防止 owner 撤銷自己
    if current_user["role"] != "admin" and current_user["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能撤銷自己的 owner 授權")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM project_members WHERE project_id=%s AND user_id=%s",
            (project_id, user_id), prepare=False,
        )
        deleted = cur.rowcount

    if deleted == 0:
        raise HTTPException(status_code=404, detail="找不到該成員授權")
    return {"ok": True, "project_id": project_id, "user_id": user_id}
