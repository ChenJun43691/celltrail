from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import (
    create_access_token,
    get_current_user,
    get_user_by_username,
    hash_password,
    verify_password,
    verify_password_db,
)
from app.services.limiter import limiter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
@limiter.limit("10/5minutes")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    """
    Content-Type: application/x-www-form-urlencoded
    username=...&password=...

    回傳：
      access_token, token_type,
      must_change_password（True 時前端應強制導向修改密碼頁）
    """
    user = get_user_by_username(form.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="帳號已停用，請聯絡管理員")

    ok = False
    ph = user.get("password_hash") or ""
    if ph:
        ok = verify_password(form.password, ph)
        if not ok:
            ok = verify_password_db(user["username"], form.password)

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token({"sub": user["username"]})
    return {
        "access_token": token,
        "token_type": "bearer",
        "must_change_password": user.get("must_change_password", False),
    }


@router.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    return {
        "id":                   current_user["id"],
        "username":             current_user["username"],
        "role":                 current_user["role"],
        "real_name":            current_user.get("real_name"),
        "unit":                 current_user.get("unit"),
        "badge_number":         current_user.get("badge_number"),
        "email":                current_user.get("email"),
        "must_change_password": current_user.get("must_change_password", False),
    }


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password:     str = Field(min_length=8, max_length=128,
                                  description="新密碼至少 8 字元")


@router.post("/change-password")
@limiter.limit("5/5minutes")
def change_password(
    request: Request,
    payload: ChangePasswordIn,
    current_user: dict = Depends(get_current_user),
):
    """
    修改自己的密碼。
    - 首次登入（must_change_password=True）強制通過此端點後才能正常使用系統。
    - current_password 必須正確（防止 session 被盜用後無聲竄改密碼）。
    """
    # 重新查一次確保拿到最新 password_hash（JWT 快取問題）
    db_user = get_user_by_username(current_user["username"])
    if not db_user:
        raise HTTPException(status_code=404, detail="帳號不存在")

    ph = db_user.get("password_hash") or ""
    ok = verify_password(payload.current_password, ph)
    if not ok:
        ok = verify_password_db(db_user["username"], payload.current_password)
    if not ok:
        raise HTTPException(status_code=400, detail="目前密碼錯誤")

    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="新密碼不能與目前密碼相同")

    new_hash = hash_password(payload.new_password)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users
               SET password_hash = %s,
                   must_change_password = FALSE,
                   updated_at = now()
             WHERE id = %s
            """,
            (new_hash, current_user["id"]),
            prepare=False,
        )

    return {"ok": True, "message": "密碼已更新"}
