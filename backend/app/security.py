import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logging.getLogger("passlib").setLevel(logging.ERROR)
logging.getLogger("passlib.handlers.bcrypt").setLevel(logging.ERROR)

from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.db.session import get_conn

# ===== JWT =====
# JWT 簽章金鑰。同時接受 SECRET_KEY 與 JWT_SECRET 兩種環境變數名 —— 雲端
# （Render）部署慣用 JWT_SECRET，若程式只讀 SECRET_KEY 會 fallback 到下方公開
# 預設值，導致任何人都能用該預設值偽造 admin token（2026-06-13 實測雲端中招）。
# 保留 fallback 字串只為本機開發；main.py 啟動自檢會在 AUTH_ENABLED 下以此為
# 預設值時 fail-fast，拒絕以可偽造的密鑰對外服務。
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET") or "change-me-please"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 12 * 60  # 12 小時（一個工作天）

pwd_context = CryptContext(
    schemes=["bcrypt", "pbkdf2_sha256"],
    deprecated="auto",
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_user_by_username(username: str) -> Optional[dict]:
    sql = """
    SELECT id, username, password_hash, role,
           is_active, must_change_password,
           real_name, unit, badge_number, email
      FROM users WHERE username = %s
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (username,), prepare=False)
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "username": row[1], "password_hash": row[2], "role": row[3],
        "is_active": row[4] if row[4] is not None else True,
        "must_change_password": row[5] if row[5] is not None else False,
        "real_name": row[6], "unit": row[7], "badge_number": row[8], "email": row[9],
    }


def verify_password_db(username: str, plain: str) -> bool:
    sql = "SELECT crypt(%s, password_hash) = password_hash AS ok FROM users WHERE username=%s"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (plain, username), prepare=False)
        row = cur.fetchone()
        return bool(row and row[0])


# ============================================================
# 身份驗證總開關
# ============================================================
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
if not AUTH_ENABLED:
    logging.getLogger("celltrail.security").warning(
        "AUTH_ENABLED=false 已啟用：所有請求將以 anonymous admin 身份通行。"
        " 若這是 production 環境，請立即在 .env 設 AUTH_ENABLED=true 並重啟。"
    )

_ANONYMOUS_ADMIN = {
    "id": 0, "username": "anonymous", "role": "admin",
    "is_active": True, "must_change_password": False,
    "real_name": None, "unit": None, "badge_number": None, "email": None,
}


def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    if not AUTH_ENABLED:
        return dict(_ANONYMOUS_ADMIN)  # 回傳複本，避免呼叫端 mutate 汙染共用範本

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
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="帳號已停用，請聯絡管理員")
    return user


def get_current_user_optional(token: Optional[str] = Depends(oauth2_scheme)) -> Optional[dict]:
    """
    類似 get_current_user 但無 token 或 token 無效時回 None（不拋 401）。
    供「訪客也能用」的端點使用（例如格式回報）。
    """
    if not AUTH_ENABLED:
        return dict(_ANONYMOUS_ADMIN)  # 回傳複本，避免呼叫端 mutate 汙染共用範本
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if not username:
            return None
    except JWTError:
        return None
    user = get_user_by_username(username)
    if not user or not user.get("is_active", True):
        return None
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return user


# ============================================================
# 專案層級權限檢查
# ============================================================
_PERM_LEVELS = {"viewer": 0, "collaborator": 1, "owner": 2}


def assert_project_access(user: dict, project_id: str, min_permission: str = "viewer") -> None:
    """
    若 user 對 project_id 不具備 min_permission 以上的權限則拋 403。
    - AUTH_ENABLED=false → anonymous admin → 直接通過
    - system admin (role='admin') → 直接通過
    - 其他 → 查 project_members，驗 permission 層級與有效期
    - project 尚無任何成員（全新 project）→ 403（需先由 admin 授權或 upload 時自動賦予 owner）

    Permission 層級：viewer(0) < collaborator(1) < owner(2)
    """
    if not AUTH_ENABLED or user["role"] == "admin":
        return

    required = _PERM_LEVELS.get(min_permission, 0)
    sql = """
    SELECT permission FROM project_members
     WHERE project_id = %s
       AND user_id    = %s
       AND (expires_at IS NULL OR expires_at > now())
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id, user["id"]), prepare=False)
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=403, detail="無此案件的存取權限")
    if _PERM_LEVELS.get(row[0], -1) < required:
        raise HTTPException(
            status_code=403,
            detail=f"需要 {min_permission} 以上權限（目前：{row[0]}）",
        )


def add_project_member(project_id: str, user_id: int, permission: str, granted_by: Optional[int] = None) -> None:
    """
    新增或更新 project 成員。用於首次上傳時自動賦予 owner。
    使用 ON CONFLICT DO UPDATE 確保冪等。
    """
    sql = """
    INSERT INTO project_members (project_id, user_id, permission, granted_by)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (project_id, user_id)
    DO UPDATE SET permission = EXCLUDED.permission,
                  granted_by = EXCLUDED.granted_by
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id, user_id, permission, granted_by), prepare=False)


def project_has_members(project_id: str) -> bool:
    """檢查 project 是否已有任何有效成員（決定是否走「新 project 自動授權 owner」邏輯）。"""
    sql = """
    SELECT 1 FROM project_members
     WHERE project_id = %s
       AND (expires_at IS NULL OR expires_at > now())
     LIMIT 1
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id,), prepare=False)
        return cur.fetchone() is not None
