# backend/app/api/members.py
"""
Project 成員授權管理

端點（需登入）：
  GET    /api/projects/                                  列出有權限的案件
  DELETE /api/projects/{project_id}                      軟刪整個案件（owner/admin）
  GET    /api/projects/{project_id}/members              列出成員（owner/admin）
  POST   /api/projects/{project_id}/members              授權成員（owner/admin）
  DELETE /api/projects/{project_id}/members/{user_id}   撤銷授權（owner/admin）
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import (
    assert_project_access,
    get_current_user,
    require_admin,
)
from app.services.audit import write_audit

router = APIRouter(tags=["members"])


# ---------- 列出目前使用者有權限的所有案件 ----------
@router.get("/projects/")
def list_projects(current_user: dict = Depends(get_current_user)):
    """
    回傳目前登入者有權限的所有案件清單。
    - 管理員：回傳 raw_traces 內所有 project（不含 __temp_ 前綴）
    - 一般使用者：回傳 project_members 中有效授權的案件

    回傳格式：[{ project_id, created_at, member_count, permission }]
    permission：呼叫者對該案件的權限（admin → 'admin'；一般使用者 → 其
                project_members.permission）。前端據此決定是否顯示刪除鈕。

    註：兩個分支都只列「仍有未軟刪 raw_traces」的案件 —— 整個案件被
        軟刪後（所有 raw_traces.deleted_at 已設）即不再出現於清單。
    """
    if current_user.get("role") == "admin":
        sql = """
        WITH rt AS (
            SELECT DISTINCT project_id
              FROM raw_traces
             WHERE project_id NOT LIKE '%%__temp_%%'
               AND deleted_at IS NULL
        ),
        pm AS (
            SELECT project_id,
                   MIN(created_at)          AS first_at,
                   COUNT(DISTINCT user_id)  AS member_count
              FROM project_members
             GROUP BY project_id
        )
        SELECT rt.project_id,
               pm.first_at,
               COALESCE(pm.member_count, 0) AS member_count,
               'admin'::text                AS permission
          FROM rt
          LEFT JOIN pm ON rt.project_id = pm.project_id
         ORDER BY COALESCE(pm.first_at, '2000-01-01'::timestamptz) DESC
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, prepare=False)
            rows = cur.fetchall()
    else:
        sql = """
        SELECT pm.project_id,
               MIN(pm.created_at)  AS first_at,
               COUNT(DISTINCT pm2.user_id) AS member_count,
               pm.permission
          FROM project_members pm
          LEFT JOIN project_members pm2 ON pm2.project_id = pm.project_id
         WHERE pm.user_id = %s
           AND (pm.expires_at IS NULL OR pm.expires_at > now())
           AND EXISTS (
               SELECT 1 FROM raw_traces rt
                WHERE rt.project_id = pm.project_id
                  AND rt.deleted_at IS NULL
           )
         GROUP BY pm.project_id, pm.permission
         ORDER BY MIN(pm.created_at) DESC
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (current_user["id"],), prepare=False)
            rows = cur.fetchall()

    return [
        {
            "project_id":   r[0],
            "created_at":   r[1].isoformat() if r[1] else None,
            "member_count": int(r[2]),
            "permission":   r[3],
        }
        for r in rows
    ]


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


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    軟刪整個案件：把該 project_id 底下所有未刪除的 raw_traces 標記
    deleted_at = now()。需 owner 或 admin。

    - 採軟刪（不 DELETE 實體列）：raw_traces 是刑事證據，保留可回溯。
    - 寫一筆 audit_logs（action=project.delete）。
    - 軟刪後該案件即不再出現於 GET /projects/（清單已過濾 deleted_at）。
    """
    _require_project_owner(project_id, current_user)

    # id=0 = anonymous admin（AUTH_ENABLED=false），不在 users 表 → FK 用 NULL
    deleter_id = current_user["id"] if current_user.get("id") else None

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raw_traces
                   SET deleted_at    = now(),
                       deleted_by    = %s,
                       delete_reason = %s
                 WHERE project_id = %s
                   AND deleted_at IS NULL
                """,
                (deleter_id, "整個案件刪除", project_id),
                prepare=False,
            )
            deleted = cur.rowcount
    except Exception as e:
        write_audit(
            action="project.delete_failed",
            user=current_user, request=request,
            target_type="project", target_ref=project_id, project_id=project_id,
            status_code=500, error_text=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(status_code=500, detail=f"刪除失敗：{type(e).__name__}: {e}")

    if deleted == 0:
        # 沒有任何未刪除的 raw_traces → 案件不存在或已刪除
        raise HTTPException(status_code=404, detail="案件不存在或已刪除")

    write_audit(
        action="project.delete",
        user=current_user, request=request,
        target_type="project", target_ref=project_id, project_id=project_id,
        details={"affected_rows": deleted},
        status_code=200,
    )
    return {"ok": True, "project_id": project_id, "affected_rows": deleted}
