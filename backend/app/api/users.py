# backend/app/api/users.py
"""
使用者管理 API（僅限 admin）

端點：
  POST   /api/users/                  建立帳號（admin 代建，系統產臨時密碼）
  GET    /api/users/                  列出所有帳號
  PATCH  /api/users/{id}              更新角色或密碼（admin 強制重設）
  PATCH  /api/users/{id}/deactivate   停用帳號
  PATCH  /api/users/{id}/reactivate   恢復帳號

設計原則：
  - 不開放自行註冊；由 admin 在後台建立帳號
  - 建立時系統產生 16 字元臨時密碼，must_change_password=True
  - 停用而非刪除（保留 audit_logs 裡的 user_id 可回溯）
"""
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import hash_password, require_admin

router = APIRouter(prefix="/users", tags=["users"])


# ---------- Pydantic Schemas ----------
class UserCreateIn(BaseModel):
    username:     str           = Field(min_length=1, max_length=64)
    real_name:    Optional[str] = Field(default=None, max_length=64)
    unit:         Optional[str] = Field(default=None, max_length=64)
    badge_number: Optional[str] = Field(default=None, max_length=32)
    email:        Optional[str] = Field(default=None, max_length=128)
    role:         str           = Field(default="user", pattern="^(admin|user)$")


class UserUpdateIn(BaseModel):
    password:       Optional[str]  = Field(default=None, min_length=8, max_length=128,
                                           description="admin 強制設定密碼（設後 must_change_password=True）")
    reset_password: Optional[bool] = Field(default=None,
                                           description="True=系統產生新臨時密碼並回傳（僅一次）")
    role:           Optional[str]  = Field(default=None, pattern="^(admin|user)$")


class UserOut(BaseModel):
    id:           int
    username:     str
    role:         str
    real_name:    Optional[str]
    unit:         Optional[str]
    badge_number: Optional[str]
    email:        Optional[str]
    is_active:    bool
    must_change_password: bool


def _row_to_user(r) -> dict:
    return {
        "id": r[0], "username": r[1], "role": r[2],
        "real_name": r[3], "unit": r[4], "badge_number": r[5], "email": r[6],
        "is_active": r[7] if r[7] is not None else True,
        "must_change_password": r[8] if r[8] is not None else False,
        "created_at": r[9].isoformat() if len(r) > 9 and r[9] else None,
    }


# ---------- Endpoints ----------
@router.post("", dependencies=[Depends(require_admin)])
def create_user(payload: UserCreateIn):
    """
    建立帳號。系統自動產生臨時密碼（16 字元），must_change_password=True。
    回傳中包含 temp_password（只此一次，請當面或安全管道告知使用者）。
    """
    temp_password = secrets.token_urlsafe(12)  # ~16 char
    pwd_hash = hash_password(temp_password)

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users
                    (username, password_hash, role,
                     real_name, unit, badge_number, email,
                     is_active, must_change_password)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, TRUE)
                RETURNING id, username, role, real_name, unit, badge_number, email,
                          is_active, must_change_password
                """,
                (payload.username, pwd_hash, payload.role,
                 payload.real_name, payload.unit, payload.badge_number, payload.email),
                prepare=False,
            )
            row = cur.fetchone()
    except Exception as e:
        msg = str(e)
        if "duplicate key" in msg or "UniqueViolation" in msg:
            raise HTTPException(status_code=409, detail="使用者名稱已存在")
        raise HTTPException(status_code=400, detail=f"建立失敗：{type(e).__name__}: {e}")

    result = _row_to_user(row)
    result["temp_password"] = temp_password  # 只在建立時回傳
    return result


@router.get("", dependencies=[Depends(require_admin)])
def list_users():
    """列出所有帳號（不含密碼）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, username, role, real_name, unit, badge_number, email,
                   is_active, must_change_password, created_at
              FROM users
             ORDER BY id
            """,
            prepare=False,
        )
        rows = cur.fetchall()

    return {
        "total": len(rows),
        "items": [_row_to_user(r) for r in rows],
    }


@router.patch("/{user_id}", dependencies=[Depends(require_admin)])
def update_user(user_id: int, payload: UserUpdateIn,
                current_admin: dict = Depends(require_admin)):
    """更新角色或強制重設密碼（重設後 must_change_password=True）。"""
    if payload.password is None and payload.role is None and not payload.reset_password:
        raise HTTPException(status_code=400, detail="至少需提供 password、reset_password 或 role")

    temp_password: str | None = None
    sets: list[str] = ["updated_at = now()"]
    params: list = []

    if payload.reset_password:
        temp_password = secrets.token_urlsafe(12)
        sets.insert(0, "password_hash = %s")
        sets.insert(1, "must_change_password = TRUE")
        params.append(hash_password(temp_password))
    elif payload.password is not None:
        sets.insert(0, "password_hash = %s")
        sets.insert(1, "must_change_password = TRUE")
        params.append(hash_password(payload.password))

    if payload.role is not None:
        if payload.role == "user" and current_admin["id"] == user_id:
            raise HTTPException(status_code=400, detail="不能降級自己的 admin 身份")
        sets.insert(0, "role = %s")
        params.append(payload.role)
    params.append(user_id)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, username, role, real_name, unit, badge_number, email, is_active, must_change_password",
            params, prepare=False,
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="使用者不存在")
    result = _row_to_user(row)
    if temp_password:
        result["temp_password"] = temp_password
    return result


@router.patch("/{user_id}/deactivate", dependencies=[Depends(require_admin)])
def deactivate_user(user_id: int, current_admin: dict = Depends(require_admin)):
    """停用帳號（不刪除，保留 audit trail）。"""
    if current_admin["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能停用自己的帳號")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_active=FALSE, updated_at=now() WHERE id=%s "
            "RETURNING id, username",
            (user_id,), prepare=False,
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="使用者不存在")
    return {"ok": True, "id": row[0], "username": row[1], "is_active": False}


@router.patch("/{user_id}/reactivate", dependencies=[Depends(require_admin)])
def reactivate_user(user_id: int):
    """恢復已停用的帳號。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_active=TRUE, updated_at=now() WHERE id=%s "
            "RETURNING id, username",
            (user_id,), prepare=False,
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="使用者不存在")
    return {"ok": True, "id": row[0], "username": row[1], "is_active": True}
