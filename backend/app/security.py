# app/security.py
import os
import datetime as dt
from typing import Optional

from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.db.session import pool  # 你現有的連線池

# 從環境變數讀取金鑰；務必改成長且隨機的字串
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 8 * 60  # token 有效 8 小時

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


# ----- 密碼雜湊/驗證 -----
def hash_password(plain: str) -> str:
    """把明碼變成 PBKDF2-SHA256 雜湊字串，存進資料庫。"""
    return pwd_context.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    """登入時驗證密碼是否正確（支援 PBKDF2-SHA256）。"""
    return pwd_context.verify(plain, hashed)


# ----- JWT 簽發 -----
def create_access_token(data: dict, expires_delta: Optional[dt.timedelta] = None) -> str:
    """簽一顆存有 'sub'=username 的 JWT。"""
    to_encode = data.copy()
    expire = dt.datetime.utcnow() + (expires_delta or dt.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ----- DB 取用戶 -----
def get_user_by_username(username: str):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username=%s",
            (username,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2], "role": row[3]}


# ----- 依賴：取得目前登入者 / 僅限管理員 -----
def get_current_user(token: str = Depends(oauth2_scheme)):
    """從 Bearer Token 還原使用者；驗證失敗就 401。"""
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="無效或過期的 Token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise cred_exc
    except JWTError:
        raise cred_exc

    user = get_user_by_username(username)
    if not user:
        raise cred_exc
    return user


def require_admin(user=Depends(get_current_user)):
    """限制只有 role=admin 可通過。"""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return user