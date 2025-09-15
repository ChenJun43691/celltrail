from fastapi import APIRouter, Query
from typing import Optional
from app.db.session import pool

router = APIRouter()

@router.get("/projects/{project_id}/map-layers")
def project_map_layers(project_id: str, target_id: Optional[str] = None, limit: int = Query(2000, le=10000)):
    params = [project_id]
    where = "project_id = %s AND geom IS NOT NULL"
    if target_id:
        where += " AND target_id = %s"
        params.append(target_id)

    sql = f"""
    SELECT id, target_id, start_ts, end_ts, cell_id, cell_addr, azimuth, accuracy_m,
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
            for (rid, tid, st, et, cid, addr, az, acc, lng, lat) in cur.fetchall():
                feats.append({
                    "type":"Feature",
                    "geometry":{"type":"Point","coordinates":[float(lng), float(lat)]},
                    "properties":{
                        "id": rid, "target_id": tid,
                        "start_ts": st.isoformat(), "end_ts": et.isoformat(),
                        "cell_id": cid, "cell_addr": addr, "azimuth": az, "accuracy_m": acc
                    }
                })
    return {"type":"FeatureCollection", "features": feats}