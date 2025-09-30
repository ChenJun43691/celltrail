# app/api/auth.py
from fastapi import APIRouter, Depends, HTTPException, Form
from pydantic import BaseModel
from app.security import create_access_token, get_current_user, require_admin
from app.db.session import pool

router = APIRouter()

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

@router.post("/auth/login", response_model=Token)
def login(username: str = Form(...), password: str = Form(...)):
    """
    用帳密換取 JWT（由 Postgres/pgcrypto 驗證密碼）
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, username, role
            FROM users
            WHERE username = %s
              AND password_hash = crypt(%s, password_hash)
        """, (username, password))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    token = create_access_token({"sub": username})
    return {"access_token": token, "token_type": "bearer"}

@router.get("/auth/me")
def me(user = Depends(get_current_user)):
    """回傳目前登入者（讓前端知道角色用）"""
    return {"id": user["id"], "username": user["username"], "role": user["role"]}

@router.post("/auth/register", dependencies=[Depends(require_admin)])
def register(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
):
    """
    由管理員建立新使用者；密碼雜湊交給 Postgres/pgcrypto
    """
    if role not in ("admin", "user"):
        raise HTTPException(400, "role 必須為 admin 或 user")

    with pool.connection() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (%s, crypt(%s, gen_salt('bf')), %s)",
                (username, password, role),
            )
            conn.commit()
        except Exception as e:
            raise HTTPException(400, f"建立使用者失敗：{e}")
    return {"ok": True, "username": username, "role": role}