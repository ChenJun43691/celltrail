# backend/app/api/users.py
"""
使用者管理 API（僅限 admin）

端點：
  - POST   /api/users            建立使用者
  - GET    /api/users            列出所有使用者
  - PATCH  /api/users/{id}       更新使用者（角色 / 密碼）
  - DELETE /api/users/{id}       刪除使用者
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import hash_password, require_admin

router = APIRouter(prefix="/users", tags=["users"])


# ---------- Pydantic Schemas ----------
class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = Field(default="user", pattern="^(admin|user)$")


class UserUpdateIn(BaseModel):
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
    role: Optional[str] = Field(default=None, pattern="^(admin|user)$")


class UserOut(BaseModel):
    id: int
    username: str
    role: str


# ---------- Endpoints ----------
@router.post("", response_model=UserOut, dependencies=[Depends(require_admin)])
def create_user(payload: UserCreateIn):
    """建立使用者（僅 admin 可呼叫）。"""
    pwd = hash_password(payload.password)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, role)
                VALUES (%s, %s, %s)
                RETURNING id, username, role
                """,
                (payload.username, pwd, payload.role),
                prepare=False,
            )
            row = cur.fetchone()
    except Exception as e:
        # username UNIQUE 衝突時，pg 會丟 UniqueViolation
        msg = f"{type(e).__name__}: {e}"
        if "duplicate key" in msg or "UniqueViolation" in msg:
            raise HTTPException(status_code=409, detail="使用者名稱已存在")
        raise HTTPException(status_code=400, detail=f"建立失敗：{msg}")

    return {"id": row[0], "username": row[1], "role": row[2]}


@router.get("", dependencies=[Depends(require_admin)])
def list_users():
    """列出所有使用者（不含 password_hash）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY id",
            prepare=False,
        )
        rows = cur.fetchall()

    return {
        "total": len(rows),
        "items": [
            {
                "id": r[0],
                "username": r[1],
                "role": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ],
    }


@router.patch("/{user_id}", response_model=UserOut, dependencies=[Depends(require_admin)])
def update_user(user_id: int, payload: UserUpdateIn):
    """更新使用者的密碼或角色。至少需要一個欄位。"""
    if payload.password is None and payload.role is None:
        raise HTTPException(status_code=400, detail="至少需提供 password 或 role")

    sets: list[str] = []
    params: list = []
    if payload.password is not None:
        sets.append("password_hash = %s")
        params.append(hash_password(payload.password))
    if payload.role is not None:
        sets.append("role = %s")
        params.append(payload.role)
    sets.append("updated_at = now()")
    params.append(user_id)

    sql = f"""
        UPDATE users SET {", ".join(sets)}
        WHERE id = %s
        RETURNING id, username, role
    """

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="使用者不存在")

    return {"id": row[0], "username": row[1], "role": row[2]}


@router.delete("/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: int, current_admin: dict = Depends(require_admin)):
    """刪除使用者。禁止刪除自己，避免把最後一個 admin 刪光。"""
    if current_admin["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能刪除自己")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM users WHERE id = %s",
            (user_id,),
            prepare=False,
        )
        deleted = cur.rowcount

    if deleted == 0:
        raise HTTPException(status_code=404, detail="使用者不存在")

    return {"ok": True, "deleted": deleted, "id": user_id}
