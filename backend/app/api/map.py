# app/api/map.py
from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from app.db.session import pool

router = APIRouter()

# 取得地圖圖層（GeoJSON）
@router.get("/projects/{project_id}/map-layers")
def project_map_layers(
    project_id: str,
    target_id: Optional[str] = None,
    limit: int = Query(2000, le=10000)
):
    params = [project_id]
    where = "project_id = %s AND geom IS NOT NULL"
    if target_id:
        where += " AND target_id = %s"
        params.append(target_id)

    sql = f"""
        SELECT target_id, start_ts, end_ts, cell_id, cell_addr, azimuth, accuracy_m,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM raw_traces
        WHERE {where}
        ORDER BY start_ts ASC
        LIMIT %s
    """
    params.append(limit)

    feats = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for (tid, st, et, cid, addr, az, acc, lng, lat) in cur.fetchall():
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(lng), float(lat)]},
                    "properties": {
                        "target_id": tid,
                        "start_ts": (st.isoformat() if st else None),
                        "end_ts": (et.isoformat() if et else None),
                        "cell_id": cid,
                        "cell_addr": addr,
                        "azimuth": az,
                        "accuracy_m": acc,
                    }
                })
    return {"type": "FeatureCollection", "features": feats}

# 刪除某個 target 的所有紀錄
@router.delete("/projects/{project_id}/targets/{target_id}")
def delete_target(project_id: str, target_id: str):
    """
    刪除 raw_traces 中某專案 + 某 target 的全部資料列。
    成功回傳刪除筆數；若找不到資料則回 404。
    """
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM raw_traces
                    WHERE project_id = %s AND target_id = %s
                    """,
                    (project_id, target_id),
                )
                deleted = cur.rowcount
            conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刪除失敗：{type(e).__name__}: {e}")

    if deleted == 0:
        raise HTTPException(status_code=404, detail="Target 不存在或已刪除")

    return {"ok": True, "deleted": deleted, "project_id": project_id, "target_id": target_id}