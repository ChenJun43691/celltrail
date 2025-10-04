# backend/app/security.py
import os
import datetime as dt
from typing import Optional

from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.db.session import get_conn  # 連線池

# ----- JWT -----
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 8 * 60  # 8 小時

# 同時支援 bcrypt 與 pbkdf2_sha256（舊資料也能驗）
pwd_context = CryptContext(
    schemes=["bcrypt", "pbkdf2_sha256"],
    deprecated="auto",
)

# 注意：tokenUrl 必須對應到 main.py 的路由前綴（/api + /auth/login）
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """優先用 passlib 驗；有例外或不相容就回 False（由呼叫端 fallback）。"""
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[dt.timedelta] = None) -> str:
    to_encode = data.copy()
    expire = dt.datetime.utcnow() + (expires_delta or dt.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_user_by_username(username: str):
    sql = "SELECT id, username, password_hash, role FROM users WHERE username=%s"
    with get_conn() as conn, conn.cursor() as cur:
        # psycopg3 沒有 prepare 參數；關閉 prepared statements 已在 get_conn() 完成
        cur.execute(sql, (username,))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "role": row[3]}


def verify_password_db(username: str, plain: str) -> bool:
    """
    用 Postgres 端的 pgcrypto/crypt() 驗密，確保與 DB 內 hash 100% 相容。
    """
    sql = "SELECT crypt(%s, password_hash) = password_hash AS ok FROM users WHERE username=%s"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (plain, username))
        row = cur.fetchone()
        return bool(row and row[0])


def get_current_user(token: str = Depends(oauth2_scheme)):
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="無效或過期的 Token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if not username:
            raise cred_exc
    except JWTError:
        raise cred_exc

    user = get_user_by_username(username)
    if not user:
        raise cred_exc
    return user


def require_admin(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return user