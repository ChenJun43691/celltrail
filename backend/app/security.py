import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── 抑制 passlib 1.7.4 在 bcrypt 4.x 下吐的 "(trapped) error reading bcrypt version"
# 原因：passlib 1.7.4 讀 bcrypt.__about__.__version__，但 bcrypt 4.x 已移除該屬性。
# passlib 內部 try/except 後會 fallback 成功，所以是「警告級」雜訊，非實際錯誤。
# 等 passlib 1.8 釋出後可移除這兩行。
logging.getLogger("passlib").setLevel(logging.ERROR)
logging.getLogger("passlib.handlers.bcrypt").setLevel(logging.ERROR)

from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.db.session import get_conn  # 連線池

# ===== JWT 基本設定 =====
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 8 * 60  # 8 小時

# 同時支援 bcrypt 與 pbkdf2_sha256（保留舊資料相容性）
pwd_context = CryptContext(
    schemes=["bcrypt", "pbkdf2_sha256"],
    deprecated="auto",
)

# 前端用 /api/auth/login 換 token
# auto_error=False：沒帶 token 時不自動丟 401，而是把 token 設為 None 交給 get_current_user 自行判斷
# 配合下方「關閉身份驗證」機制，讓未登入呼叫也能通過
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """優先用 passlib 驗；遇到例外就回 False（呼叫端可再用 DB 端 crypt() 補驗）"""
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    建立 JWT access token。

    使用 timezone-aware datetime（UTC），因 Python 3.12 起 datetime.utcnow() 已 deprecated。
    jose 會自動把 tz-aware 的 datetime 轉為 UNIX timestamp 寫入 exp claim。
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_user_by_username(username: str):
    sql = "SELECT id, username, password_hash, role FROM users WHERE username=%s"
    with get_conn() as conn, conn.cursor() as cur:
        # 關鍵：針對 execute 明確關閉 prepared（pooler 友善）
        cur.execute(sql, (username,), prepare=False)
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "role": row[3]}


def verify_password_db(username: str, plain: str) -> bool:
    """
    使用 Postgres 端 pgcrypto/crypt() 驗密，確保與 DB 內 hash 100% 相容。
    """
    sql = "SELECT crypt(%s, password_hash) = password_hash AS ok FROM users WHERE username=%s"
    with get_conn() as conn, conn.cursor() as cur:
        # 同樣關掉 prepared
        cur.execute(sql, (plain, username), prepare=False)
        row = cur.fetchone()
        return bool(row and row[0])


# ============================================================
# 身份驗證總開關
# ------------------------------------------------------------
# AUTH_ENABLED=true（預設，2026-04-26 變更）→ 走完整 JWT 驗證
# AUTH_ENABLED=false                       → 匿名通行，任何請求視為 admin
#
# 為什麼預設改 true：
#   1. 法庭可防禦性：證物系統若預設裸奔，audit log 內 user_id=0/anonymous
#      佔多數，等於失去「可究責性」，被告律師可主張紀錄不可信
#   2. 偵查機密：raw_traces 內含未公開案件嫌疑人位置軌跡，外洩風險不可承擔
#   3. CTF/red team 風險：開源/部署到雲端時若預設 false，相當於把 admin
#      端點直接暴露給網際網路
#
# 為什麼保留開關而不是直接拆掉：
#   1. 路由簽章（Depends(get_current_user)、Depends(require_admin)）完全不動
#   2. 純本機開發 / pytest 整合測試時，可在環境內 export AUTH_ENABLED=false 跳過
#   3. Production 應「絕對不要」設 false（請參考 .env.example 警示）
# ============================================================
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
if not AUTH_ENABLED:
    # 啟動時印一行明顯警告，讓 ops 看到 log 立刻意識到風險
    logging.getLogger("celltrail.security").warning(
        "AUTH_ENABLED=false 已啟用：所有請求將以 anonymous admin 身份通行。"
        " 若這是 production 環境，請立即在 .env 設 AUTH_ENABLED=true 並重啟。"
    )

# 匿名使用者的預設身份，關閉驗證時所有端點都視為 admin 通過
_ANONYMOUS_ADMIN = {"id": 0, "username": "anonymous", "role": "admin"}


def get_current_user(token: Optional[str] = Depends(oauth2_scheme)):
    # ── 若關閉身份驗證：無條件回傳匿名 admin
    if not AUTH_ENABLED:
        return _ANONYMOUS_ADMIN

    # ── 以下為原本的 JWT 驗證流程，AUTH_ENABLED=true 時才會執行
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="無效或過期的 Token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise cred_exc
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
    # 關閉驗證時 get_current_user 已回傳 admin，這裡自然通過
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return user