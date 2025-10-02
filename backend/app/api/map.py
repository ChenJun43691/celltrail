# app/api/map.py
from fastapi import APIRouter, Query, Depends
from typing import Optional
from app.db.session import get_conn
from app.security import get_current_user

router = APIRouter()

@router.get("/projects/{project_id}/map-layers",
            dependencies=[Depends(get_current_user)])  # ← 登入即可瀏覽地圖資料
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
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
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