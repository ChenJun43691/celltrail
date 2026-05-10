# backend/app/api/requests.py
"""
帳號申請流程

公開端點（不需登入）：
  POST /api/account-requests            提交申請
  GET  /api/account-requests/check-phone?phone=...   查詢電話是否有待審/已核准記錄

Admin 端點（需 admin）：
  GET    /api/account-requests          列出申請（預設只列 pending）
  POST   /api/account-requests/{id}/approve   核准 → 自動建帳號 + 回傳臨時密碼
  POST   /api/account-requests/{id}/reject    拒絕 → 填拒絕原因
"""
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import get_current_user, hash_password, require_admin
from app.services.limiter import limiter

router = APIRouter(prefix="/account-requests", tags=["account-requests"])


# ---------- Schemas ----------
class AccountRequestIn(BaseModel):
    username:  str = Field(min_length=1, max_length=64,
                           description="申請的帳號名稱（英數字）",
                           pattern=r"^[A-Za-z0-9_\-]+$")
    real_name: str = Field(min_length=1, max_length=64)
    unit:      str = Field(min_length=1, max_length=64)
    phone:     str = Field(min_length=5, max_length=20)


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


# ---------- 公開端點 ----------
@router.post("", status_code=201)
@limiter.limit("5/hour")
def submit_request(request: Request, payload: AccountRequestIn):
    """提交帳號申請。同一電話若已有 pending 或 approved 記錄則拒絕。"""
    with get_conn() as conn, conn.cursor() as cur:
        # 防止重複申請（pending 或 approved）
        cur.execute(
            "SELECT id, status FROM account_requests WHERE phone=%s AND status IN ('pending','approved')",
            (payload.phone,), prepare=False,
        )
        dup = cur.fetchone()
        if dup:
            status_txt = "待審中" if dup[1] == "pending" else "已核准"
            raise HTTPException(
                status_code=409,
                detail=f"此電話已有申請紀錄（{status_txt}），忘記帳號密碼請洽管理員",
            )

        # 檢查帳號名稱是否已被使用
        cur.execute("SELECT id FROM users WHERE username=%s", (payload.username,), prepare=False)
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="此帳號名稱已被使用，請更換")

        cur.execute(
            """
            INSERT INTO account_requests (username, real_name, unit, phone)
            VALUES (%s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (payload.username, payload.real_name, payload.unit, payload.phone),
            prepare=False,
        )
        row = cur.fetchone()

    return {
        "ok": True,
        "id": row[0],
        "message": "申請已送出，請等候管理員審核後電話通知",
        "created_at": row[1].isoformat() if row[1] else None,
    }


@router.get("/check-phone")
def check_phone(phone: str = Query(..., min_length=5)):
    """公開查詢：此電話是否有 pending 或 approved 申請。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM account_requests WHERE phone=%s AND status IN ('pending','approved')",
            (phone,), prepare=False,
        )
        row = cur.fetchone()
    if row:
        status_txt = "待審中" if row[0] == "pending" else "已核准"
        return {"blocked": True, "status": row[0], "status_text": status_txt}
    return {"blocked": False}


# ---------- Admin 端點 ----------
@router.get("", dependencies=[Depends(require_admin)])
def list_requests(
    status: Optional[str] = Query(None, description="留空=pending；all=全部"),
):
    """列出申請清單。"""
    if status == "all":
        where = ""
        params: tuple = ()
    else:
        where = "WHERE status = 'pending'"
        params = ()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ar.id, ar.username, ar.real_name, ar.unit, ar.phone,
                   ar.status, ar.reason, ar.created_at, ar.reviewed_at,
                   u.username AS reviewed_by_name
              FROM account_requests ar
              LEFT JOIN users u ON u.id = ar.reviewed_by
             {where}
             ORDER BY ar.created_at DESC
             LIMIT 500
            """,
            params, prepare=False,
        )
        rows = cur.fetchall()

    items = []
    for r in rows:
        items.append({
            "id":               r[0],
            "username":         r[1],
            "real_name":        r[2],
            "unit":             r[3],
            "phone":            r[4],
            "status":           r[5],
            "reason":           r[6],
            "created_at":       r[7].isoformat() if r[7] else None,
            "reviewed_at":      r[8].isoformat() if r[8] else None,
            "reviewed_by_name": r[9],
        })
    return {"total": len(items), "items": items}


@router.post("/{request_id}/approve", dependencies=[Depends(require_admin)])
def approve_request(
    request_id: int,
    current_admin: dict = Depends(get_current_user),
):
    """核准申請：自動建帳號 + 產生臨時密碼，僅此一次回傳。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT username, real_name, unit FROM account_requests WHERE id=%s AND status='pending'",
            (request_id,), prepare=False,
        )
        req = cur.fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="申請不存在或狀態非 pending")

        username, real_name, unit = req

        # 再次確認 username 未被佔用
        cur.execute("SELECT id FROM users WHERE username=%s", (username,), prepare=False)
        if cur.fetchone():
            raise HTTPException(status_code=409, detail=f"帳號 {username} 已被使用，請先聯絡申請者更換帳號名稱後重試")

        temp_password = secrets.token_urlsafe(12)
        pwd_hash = hash_password(temp_password)
        reviewer_id = current_admin["id"] if current_admin["id"] != 0 else None

        cur.execute(
            """
            INSERT INTO users (username, password_hash, role, real_name, unit,
                               is_active, must_change_password)
            VALUES (%s, %s, 'user', %s, %s, TRUE, TRUE)
            RETURNING id
            """,
            (username, pwd_hash, real_name, unit), prepare=False,
        )
        new_user_id = cur.fetchone()[0]

        cur.execute(
            """
            UPDATE account_requests
               SET status='approved', reviewed_at=now(), reviewed_by=%s
             WHERE id=%s
            """,
            (reviewer_id, request_id), prepare=False,
        )

    return {
        "ok": True,
        "request_id": request_id,
        "user_id":    new_user_id,
        "username":   username,
        "real_name":  real_name,
        "temp_password": temp_password,
        "message": "帳號已建立，請電話告知申請者臨時密碼（僅顯示此一次）",
    }


@router.post("/{request_id}/reject", dependencies=[Depends(require_admin)])
def reject_request(
    request_id: int,
    payload: RejectIn,
    current_admin: dict = Depends(get_current_user),
):
    """拒絕申請，填寫拒絕原因。"""
    reviewer_id = current_admin["id"] if current_admin["id"] != 0 else None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE account_requests
               SET status='rejected', reason=%s, reviewed_at=now(), reviewed_by=%s
             WHERE id=%s AND status='pending'
            """,
            (payload.reason, reviewer_id, request_id), prepare=False,
        )
        updated = cur.rowcount
    if updated == 0:
        raise HTTPException(status_code=404, detail="申請不存在或狀態非 pending")
    return {"ok": True, "request_id": request_id, "status": "rejected"}
