# backend/app/api/map.py
import csv
import io

from fastapi import APIRouter, Query, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
from app.db.session import get_conn
from app.security import assert_project_access, get_current_user

router = APIRouter()

# ============================================================================
# 「未定位」原因分類 —— 三類，純依既有欄位推導，不需新欄位 / migration。
#
# 為什麼分這三類：
#   1. no_signal           ── cell_id 與 cell_addr 皆缺。原始檔該列殘缺，
#                            系統無從推論，需回查原始檔。
#   2. cellid_only         ── 有 cell_id 但無 cell_addr。Google/OSM 沒地址
#                            可查，必須靠業者「cell_id → 座標」對照表（即
#                            cell_towers）。匯入對照表後可重跑 geocode 救回。
#   3. addr_geocode_failed ── 有 cell_addr 但 Google/OSM 都找不到。通常是
#                            社區名 / 巷弄描述模糊。可由偵查員人工指定座標。
#
# SQL 與 Python 兩處表達需保持同義，避免 coverage 計數與 unlocated 列表不一致。
# ============================================================================
_REASON_KEYS = ("no_signal", "cellid_only", "addr_geocode_failed")

_REASON_SQL_CASE = """
CASE
  WHEN (cell_addr IS NULL OR cell_addr = '')
   AND (cell_id   IS NULL OR cell_id   = '') THEN 'no_signal'
  WHEN (cell_addr IS NULL OR cell_addr = '') THEN 'cellid_only'
  ELSE 'addr_geocode_failed'
END
"""

# --- 主要：回傳 GeoJSON（已定位：geom IS NOT NULL） ---
@router.get("/projects/{project_id}/map-layers")
def project_map_layers(
    project_id: str,
    target_id: Optional[str] = None,
    limit: int = Query(50000, ge=1, le=200000),
    current_user: dict = Depends(get_current_user),
):
    """
    依 project（可選 target）取得地圖圖層（標準 GeoJSON）。
    只回傳已定位的資料（geom IS NOT NULL）。需 viewer 以上權限。

    回應的 FeatureCollection 另附 total / returned / truncated 三欄：
    當符合條件的點數超過 limit 上限時 truncated=true，前端據此提醒使用者
    「地圖未顯示完整軌跡」，避免靜默截斷導致偵查員誤判軌跡已完整。
    """
    assert_project_access(current_user, project_id, "viewer")
    # 軟刪過濾：永遠不顯示已刪除的紀錄（deleted_at IS NULL）
    where = ["project_id = %s", "geom IS NOT NULL", "deleted_at IS NULL"]
    params = [project_id]
    if target_id:
        where.append("target_id = %s")
        params.append(target_id)
    where_sql = " AND ".join(where)

    sql = f"""
    WITH rows AS (
      SELECT
        target_id,
        start_ts,
        end_ts,
        cell_id,
        cell_addr,
        sector_name,
        site_code,
        sector_id,
        azimuth,
        azimuth_ref,
        accuracy_m,
        geom
      FROM raw_traces
      WHERE {where_sql}
      ORDER BY start_ts NULLS LAST, id
      LIMIT %s
    )
    SELECT jsonb_build_object(
      'type', 'FeatureCollection',
      'features',
        COALESCE(
          jsonb_agg(
            jsonb_build_object(
              'type', 'Feature',
              'geometry', ST_AsGeoJSON(geom)::jsonb,
              'properties', jsonb_strip_nulls(
                jsonb_build_object(
                  'target_id',   target_id,
                  'start_ts',    start_ts,
                  'end_ts',      end_ts,
                  'cell_id',     cell_id,
                  'cell_addr',   cell_addr,
                  'sector_name', sector_name,
                  'site_code',   site_code,
                  'sector_id',   sector_id,
                  'azimuth',     azimuth,
                  'azimuth_ref', azimuth_ref,
                  'accuracy_m',  accuracy_m
                )
              )
            )
          ),
          '[]'::jsonb
        )
    ) AS fc
    FROM rows;
    """
    count_sql = f"SELECT count(*) FROM raw_traces WHERE {where_sql}"

    with get_conn() as conn, conn.cursor() as cur:
        # 先取符合條件的真實總數，再取（受 limit 上限的）GeoJSON
        cur.execute(count_sql, params, prepare=False)
        total = int((cur.fetchone() or [0])[0])
        cur.execute(sql, params + [limit], prepare=False)  # ← 不使用 prepared
        row = cur.fetchone()
        fc = (row[0] if row else None) or {"type": "FeatureCollection", "features": []}

    # 把真實總數與是否截斷一併回傳，讓前端能在點數超過上限時提醒使用者，
    # 避免偵查員誤以為地圖上的軌跡已完整（GeoJSON 允許 foreign members）。
    returned = len(fc.get("features") or [])
    fc["total"] = total
    fc["returned"] = returned
    fc["truncated"] = total > returned
    return fc

# --- 聚合：定位涵蓋率（前端 L1 收據 + L2 banner 用）---
@router.get("/projects/{project_id}/coverage")
def project_coverage(
    project_id: str,
    target_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    回傳該專案（或某 target）的定位涵蓋率與未定位原因分布。

    為什麼這個端點存在：
    - /map-layers 過濾 geom IS NOT NULL → 使用者只看到「成功定位」的點。
      若上傳 300 筆但 100 筆 geocode 失敗，地圖只剩 200 點 —— 使用者會
      誤以為「資料消失了」。
    - 這個端點補上「總筆數 / 已定位 / 未定位（按原因）」三層數字，前端
      在地圖頂部常駐 banner、上傳完成 receipt 也用同一份資料，**讓
      未定位的筆數絕不沉默**。

    回應：
    {
      "project_id": "...",
      "target_id":  null,
      "total":         298,    # 該案件（軟刪後）總筆數
      "with_geom":     200,    # 已定位
      "without_geom":   98,    # 未定位
      "by_reason": {
        "no_signal":            3,   # cell_id 與 cell_addr 皆缺
        "cellid_only":         80,   # 有 cell_id 但無 cell_addr（需業者對照表）
        "addr_geocode_failed": 15    # 有 cell_addr 但 geocode 失敗
      }
    }
    需 viewer 以上權限。
    """
    assert_project_access(current_user, project_id, "viewer")

    where = ["project_id = %s", "deleted_at IS NULL"]
    params = [project_id]
    if target_id:
        where.append("target_id = %s")
        params.append(target_id)
    where_sql = " AND ".join(where)

    # 單次掃描，用 FILTER 表達 5 個欄位 —— 避免分 5 次 query 帶來不一致風險
    sql = f"""
    SELECT
      count(*) AS total,
      count(*) FILTER (WHERE geom IS NOT NULL) AS with_geom,
      count(*) FILTER (WHERE geom IS NULL)     AS without_geom,
      count(*) FILTER (WHERE geom IS NULL
                         AND (cell_addr IS NULL OR cell_addr='')
                         AND (cell_id   IS NULL OR cell_id  ='')) AS no_signal,
      count(*) FILTER (WHERE geom IS NULL
                         AND (cell_addr IS NULL OR cell_addr='')
                         AND cell_id IS NOT NULL AND cell_id<>'') AS cellid_only,
      count(*) FILTER (WHERE geom IS NULL
                         AND cell_addr IS NOT NULL AND cell_addr<>'') AS addr_geocode_failed
    FROM raw_traces
    WHERE {where_sql}
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        row = cur.fetchone() or (0, 0, 0, 0, 0, 0)
    total, with_geom, without_geom, no_sig, cid_only, addr_fail = (int(x or 0) for x in row)

    return {
        "project_id": project_id,
        "target_id":  target_id,
        "total":         total,
        "with_geom":     with_geom,
        "without_geom":  without_geom,
        "by_reason": {
            "no_signal":            no_sig,
            "cellid_only":          cid_only,
            "addr_geocode_failed":  addr_fail,
        },
    }


# --- 附加：未定位清單（方便除錯）---
@router.get("/projects/{project_id}/unlocated")
def project_unlocated_list(
    project_id: str,
    target_id: Optional[str] = None,
    reason: Optional[str] = Query(None, description="篩選原因：no_signal / cellid_only / addr_geocode_failed"),
    limit: int = Query(1000, ge=1, le=10000),
    current_user: dict = Depends(get_current_user),
):
    """
    列出 geom IS NULL 的資料，協助找出無法 geocode 的列。
    這個端點「不」回 GeoJSON；前端 L3 清單 / 除錯都用這支。需 viewer 以上權限。

    新增（2026-05-23）：
      - 每列附 reason 標籤（no_signal / cellid_only / addr_geocode_failed）
      - 可用 ?reason= 篩選單一類別，給 L3 modal 的分區檢視
    """
    assert_project_access(current_user, project_id, "viewer")
    if reason is not None and reason not in _REASON_KEYS:
        raise HTTPException(status_code=400,
                            detail=f"reason 必須是 {_REASON_KEYS} 之一")

    # 軟刪過濾：未定位清單也只看「在線」的資料
    where = ["project_id = %s", "geom IS NULL", "deleted_at IS NULL"]
    params: list = [project_id]
    if target_id:
        where.append("target_id = %s")
        params.append(target_id)
    if reason:
        where.append(f"({_REASON_SQL_CASE.strip()}) = %s")
        params.append(reason)
    where_sql = " AND ".join(where)

    sql = f"""
    SELECT id, target_id, start_ts, end_ts, cell_id, cell_addr, azimuth, accuracy_m,
           {_REASON_SQL_CASE} AS reason
    FROM raw_traces
    WHERE {where_sql}
    ORDER BY start_ts NULLS LAST, id
    LIMIT %s
    """
    params.append(limit)

    items = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        for r in cur.fetchall():
            (rid, tid, st, et, cid, addr, az, acc, reason_) = r
            items.append({
                "id": rid,
                "target_id": tid,
                "start_ts": (st.isoformat() if st else None),
                "end_ts": (et.isoformat() if et else None),
                "cell_id": cid,
                "cell_addr": addr,
                "azimuth": az,
                "accuracy_m": acc,
                "reason": reason_,
            })
    return {"total": len(items), "items": items, "filter_reason": reason}


# --- 下載：未定位清單 CSV ---
@router.get("/projects/{project_id}/unlocated.csv")
def project_unlocated_csv(
    project_id: str,
    target_id: Optional[str] = None,
    reason: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """
    把未定位清單以 CSV 下載。讓使用者把這些列拿給長官 / 業者 / 法庭時
    有一份「我們知道少了哪些，原因為何」的書面證據。需 viewer 以上權限。

    CSV 欄位：id, target_id, start_ts, end_ts, cell_id, cell_addr,
              azimuth, accuracy_m, reason
    """
    assert_project_access(current_user, project_id, "viewer")
    if reason is not None and reason not in _REASON_KEYS:
        raise HTTPException(status_code=400,
                            detail=f"reason 必須是 {_REASON_KEYS} 之一")

    where = ["project_id = %s", "geom IS NULL", "deleted_at IS NULL"]
    params: list = [project_id]
    if target_id:
        where.append("target_id = %s")
        params.append(target_id)
    if reason:
        where.append(f"({_REASON_SQL_CASE.strip()}) = %s")
        params.append(reason)
    where_sql = " AND ".join(where)

    sql = f"""
    SELECT id, target_id, start_ts, end_ts, cell_id, cell_addr,
           azimuth, accuracy_m, {_REASON_SQL_CASE} AS reason
    FROM raw_traces
    WHERE {where_sql}
    ORDER BY start_ts NULLS LAST, id
    """

    buf = io.StringIO()
    # 加 UTF-8 BOM 讓 Excel 直接開不會亂碼（偵查員多用 Excel 看 CSV）
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(["id", "target_id", "start_ts", "end_ts",
                     "cell_id", "cell_addr", "azimuth", "accuracy_m", "reason"])

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        for r in cur.fetchall():
            (rid, tid, st, et, cid, addr, az, acc, reason_) = r
            writer.writerow([
                rid, tid or "",
                (st.isoformat() if st else ""),
                (et.isoformat() if et else ""),
                cid or "", addr or "",
                "" if az is None else az,
                "" if acc is None else acc,
                reason_ or "",
            ])

    buf.seek(0)
    safe_project = project_id.replace('"', '').replace('\\', '')
    suffix = f"_{reason}" if reason else ""
    fname = f"unlocated_{safe_project}{suffix}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )