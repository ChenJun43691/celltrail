# backend/app/api/cell_towers.py
"""
本地基地台座標對照表管理（P4.1）

端點（均需 admin）：
  GET    /api/admin/cell-towers/stats   統計：筆數 / 業者分佈 / 最近匯入時間
  POST   /api/admin/cell-towers/import  匯入 CSV（cell_id,lat,lng[,carrier_name,memo]）
  DELETE /api/admin/cell-towers         清空全部（需 confirm=true query param）

CSV 格式：
  - 必填欄：cell_id, lat, lng
  - 選填欄：carrier_name, memo
  - 支援有無 header 行（偵測首行是否為純文字）
  - ON CONFLICT(cell_id) DO UPDATE → 冪等安全，可重複匯入
"""
import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.db.session import get_conn
from app.security import require_admin

router = APIRouter(prefix="/admin/cell-towers", tags=["cell-towers"])


# ---------- GET /stats ----------
@router.get("/stats")
def get_stats(_user: dict = Depends(require_admin)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cell_towers", prepare=False)
            total = cur.fetchone()[0]

            cur.execute(
                """
                SELECT carrier_name, COUNT(*) AS cnt
                FROM cell_towers
                GROUP BY carrier_name
                ORDER BY cnt DESC
                """,
                prepare=False,
            )
            carriers = [
                {"carrier_name": row[0] or "（未標示）", "count": row[1]}
                for row in cur.fetchall()
            ]

            cur.execute(
                "SELECT MAX(imported_at) FROM cell_towers",
                prepare=False,
            )
            latest = cur.fetchone()[0]

    return {
        "total": total,
        "carriers": carriers,
        "latest_import": latest.isoformat() if latest else None,
    }


# ---------- POST /import ----------
@router.post("/import")
def import_csv(
    file: UploadFile = File(...),
    carrier_name: Optional[str] = Form(default=None),
    source: Optional[str] = Form(default=None),
    user: dict = Depends(require_admin),
):
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")  # utf-8-sig 自動去掉 BOM
    except UnicodeDecodeError:
        text = raw.decode("big5", errors="replace")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "CSV 檔案為空")

    # 偵測 header：首行含非數字的 cell_id 欄或明確的欄名
    first = [c.strip().lower() for c in rows[0]]
    has_header = any(
        k in first for k in ("cell_id", "cell", "lat", "lng", "latitude", "longitude")
    ) or not _is_data_row(rows[0])
    data_rows = rows[1:] if has_header else rows

    # 如果有 header，解析欄位位置
    if has_header:
        col = {name: i for i, name in enumerate(first)}
        idx_id  = col.get("cell_id") or col.get("cell") or 0
        idx_lat = col.get("lat") or col.get("latitude") or 1
        idx_lng = col.get("lng") or col.get("longitude") or 2
        idx_carrier = col.get("carrier_name") or col.get("carrier")
        idx_memo    = col.get("memo")
    else:
        idx_id, idx_lat, idx_lng = 0, 1, 2
        idx_carrier = idx_memo = None

    inserted = updated = skipped = 0
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for lineno, row in enumerate(data_rows, start=2 if has_header else 1):
                if not row or all(c.strip() == "" for c in row):
                    continue
                try:
                    cid  = row[idx_id].strip()
                    lat  = float(row[idx_lat])
                    lng  = float(row[idx_lng])
                except (IndexError, ValueError) as e:
                    errors.append(f"第 {lineno} 行格式錯誤：{e}")
                    skipped += 1
                    continue

                if not cid:
                    errors.append(f"第 {lineno} 行 cell_id 為空，跳過")
                    skipped += 1
                    continue

                # carrier_name: Form 參數 > CSV 欄位 > None
                c_name = carrier_name
                if c_name is None and idx_carrier is not None:
                    try:
                        c_name = row[idx_carrier].strip() or None
                    except IndexError:
                        c_name = None

                memo_val = None
                if idx_memo is not None:
                    try:
                        memo_val = row[idx_memo].strip() or None
                    except IndexError:
                        pass

                cur.execute(
                    """
                    INSERT INTO cell_towers (cell_id, lat, lng, carrier_name, source, memo, imported_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cell_id) DO UPDATE
                        SET lat=EXCLUDED.lat, lng=EXCLUDED.lng,
                            carrier_name=EXCLUDED.carrier_name,
                            source=EXCLUDED.source, memo=EXCLUDED.memo,
                            imported_by=EXCLUDED.imported_by,
                            imported_at=now()
                    RETURNING (xmax = 0) AS was_insert
                    """,
                    (cid, lat, lng, c_name, source, memo_val, user["id"]),
                    prepare=False,
                )
                was_insert = cur.fetchone()[0]
                if was_insert:
                    inserted += 1
                else:
                    updated += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:20],  # 最多回傳前 20 條錯誤
    }


# ---------- DELETE / ----------
@router.delete("")
def clear_all(
    confirm: bool = Query(default=False),
    _user: dict = Depends(require_admin),
):
    if not confirm:
        raise HTTPException(400, "請加上 ?confirm=true 確認清空")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cell_towers", prepare=False)
            cur.execute("SELECT COUNT(*) FROM cell_towers", prepare=False)
            remaining = cur.fetchone()[0]
    return {"deleted": True, "remaining": remaining}


# ---------- 工具函式 ----------
def _is_data_row(row: list[str]) -> bool:
    """判斷這一行是否像資料行（前三欄都是可解析數字/數字字串）。"""
    if len(row) < 3:
        return False
    try:
        float(row[1])
        float(row[2])
        return True
    except (ValueError, IndexError):
        return False
