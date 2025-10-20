# app/api/map.py
from fastapi import APIRouter, Query, Depends
from typing import Optional
from app.db.session import get_conn
from app.security import get_current_user

router = APIRouter()

# --- 主要：回傳 GeoJSON（已定位：geom IS NOT NULL） ---
@router.get(
    "/projects/{project_id}/map-layers",
    dependencies=[Depends(get_current_user)]
)
def project_map_layers(
    project_id: str,
    target_id: Optional[str] = None,
    limit: int = Query(5000, ge=1, le=10000)
):
    """
    依 project（可選 target）取得地圖圖層（標準 GeoJSON）。
    只回傳已定位的資料（geom IS NOT NULL）。
    """
    # 用單一 SQL 在資料庫端組成 FeatureCollection，效能好
    # 注意：ST_AsGeoJSON(geom)::jsonb 可直接變成 geometry 欄
    where = ["project_id = %s", "geom IS NOT NULL"]
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

    params.append(limit)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        # row[0] 是 jsonb；psycopg3 會自動轉成 Python dict
        return row[0] or {"type": "FeatureCollection", "features": []}


# --- 附加：未定位清單（方便除錯） ---
@router.get(
    "/projects/{project_id}/unlocated",
    dependencies=[Depends(get_current_user)]
)
def project_unlocated_list(
    project_id: str,
    target_id: Optional[str] = None,
    limit: int = Query(1000, ge=1, le=10000)
):
    """
    列出 geom IS NULL 的資料，協助找出無法 geocode 的列。
    這個端點「不」回 GeoJSON；前端可做成清單提醒。
    """
    where = ["project_id = %s", "geom IS NULL"]
    params = [project_id]

    if target_id:
        where.append("target_id = %s")
        params.append(target_id)

    where_sql = " AND ".join(where)

    sql = f"""
    SELECT id, target_id, start_ts, end_ts, cell_id, cell_addr, azimuth, accuracy_m
    FROM raw_traces
    WHERE {where_sql}
    ORDER BY start_ts NULLS LAST, id
    LIMIT %s
    """

    params.append(limit)

    items = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for r in cur.fetchall():
            (rid, tid, st, et, cid, addr, az, acc) = r
            items.append({
                "id": rid,
                "target_id": tid,
                "start_ts": (st.isoformat() if st else None),
                "end_ts": (et.isoformat() if et else None),
                "cell_id": cid,
                "cell_addr": addr,
                "azimuth": az,
                "accuracy_m": acc,
            })
    return {"total": len(items), "items": items}