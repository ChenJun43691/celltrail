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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from app.db.session import get_conn
from app.security import require_admin
from app.services.audit import write_audit

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
    request: Request,
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
        idx_id  = _pick_col_req(col, "cell_id", "cell", default=0)
        idx_lat = _pick_col_req(col, "lat", "latitude", default=1)
        idx_lng = _pick_col_req(col, "lng", "longitude", default=2)
        idx_carrier = _pick_col(col, "carrier_name", "carrier")
        idx_memo    = _pick_col(col, "memo")
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

                # 座標合理性把關：寧可拒收也不要靜默寫進錯誤座標。
                # 基地台座標本身就是證據，一旦寫錯，地圖仍會畫出漂亮但錯誤的點位，
                # 事後幾乎無從察覺 —— 故此處採「拒絕該列 + 明確記錯誤」而非修正或猜測。
                if not (_LAT_MIN <= lat <= _LAT_MAX) or not (_LNG_MIN <= lng <= _LNG_MAX):
                    errors.append(
                        f"第 {lineno} 行座標超出合理範圍（lat={lat}, lng={lng}），跳過"
                        "：請確認欄位順序是否為經緯度對調"
                    )
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

    write_audit(
        action="import_cell_towers",
        user=user, request=request,
        target_type="cell_towers",
        details={
            "filename": file.filename,
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "error_count": len(errors),
            "source": source,
        },
        status_code=200,
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:20],  # 最多回傳前 20 條錯誤
    }


# ---------- DELETE / ----------
@router.delete("")
def clear_all(
    request: Request,
    confirm: bool = Query(default=False),
    user: dict = Depends(require_admin),
):
    if not confirm:
        raise HTTPException(400, "請加上 ?confirm=true 確認清空")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cell_towers", prepare=False)
            deleted_count = cur.rowcount
            cur.execute("SELECT COUNT(*) FROM cell_towers", prepare=False)
            remaining = cur.fetchone()[0]

    write_audit(
        action="clear_cell_towers",
        user=user, request=request,
        target_type="cell_towers",
        details={"deleted_count": deleted_count, "remaining": remaining},
        status_code=200,
    )
    return {"deleted": True, "remaining": remaining}


# ---------- 工具函式 ----------
def _pick_col(col: dict[str, int], *names: str, default: Optional[int] = None) -> Optional[int]:
    """依序取第一個存在的欄名，回傳其索引；都找不到才回 default。

    為什麼不能寫成 `col.get(a) or col.get(b) or default`（2026-07-21 修）：
      索引 **0 是 falsy**，欄位剛好排在第一欄時會被誤判成「找不到」而落到後備值。
      實測 header 為 `lat,lng,cell_id` 時，idx_lat 與 idx_lng 會**同時指向第 1 欄**
      → 每座基地台的緯度被寫成經度值；`lng,lat,cell_id` 則讓 idx_lng 指到 cell_id
      欄而整批列以「格式錯誤」被跳過。
    為什麼這個 bug 特別嚴重：基地台座標**本身就是證據**，錯了整條軌跡跟著錯，
      而且錯得毫無徵兆 —— 地圖照樣畫得出漂亮的點位。業者交付的 CSV 欄序不是
      我方能控制的，所以不能靠「大家都會把 cell_id 放第一欄」這種假設。
    """
    for n in names:
        if n in col:
            return col[n]
    return default


def _pick_col_req(col: dict[str, int], *names: str, default: int) -> int:
    """同 _pick_col，但保證回傳 int（給一定會有後備位置的必要欄位用）。

    刻意分成兩支而不是在呼叫端寫 `_pick_col(...) or default`：後者正是本次修掉
    的 falsy-zero bug 本身，留著這個寫法遲早有人照抄。
    """
    idx = _pick_col(col, *names)
    return default if idx is None else idx


# 座標合理範圍。除了擋純粹的髒資料，這也是上面 falsy-zero 類錯誤的第二道防線：
# 台灣的經度（約 120~122）落在合法緯度範圍 [-90, 90] 之外，故「經緯度對調」
# 這個最常見的實務失誤會在此被攔下，而不是靜默寫進 DB。
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LNG_MIN, _LNG_MAX = -180.0, 180.0


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
