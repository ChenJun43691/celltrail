from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.security import (
    create_access_token,
    get_current_user,
    get_user_by_username,
    verify_password,
    verify_password_db,
)

# 注意：這裡只用 /auth，不含 /api，/api 由 main.py 統一加上
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    """
    Content-Type: application/x-www-form-urlencoded
    username=...&password=...
    """
    user = get_user_by_username(form.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 先用 passlib（快）；不行就用 DB crypt()（最相容）
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
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me")
def me(current_user=Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "role": current_user["role"],
    }