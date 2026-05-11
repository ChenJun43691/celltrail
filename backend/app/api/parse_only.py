# backend/app/api/parse_only.py
"""
訪客解析端點（parse-only）

無需 JWT 驗證，供訪客免登入體驗使用。
解析 + geocode，但完全不寫入任何 DB 資料表。

Rate-limit：同一 IP 每小時最多 20 次。
  超過時回傳 HTTP 429（由 main.py 的 _rate_limit_exceeded_handler 統一格式化，
  前端收到 detail:"使用次數過多，請登入後繼續使用。"）

回傳格式與 /upload/parse-temp 完全相同，前端可共用同一 appendGeoJsonToSeries() 邏輯。
"""
import traceback

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.services.ingest import parse_file_only
from app.services.limiter import limiter

router = APIRouter()

_RATE_LIMIT = "20/hour"


@router.post("/parse-only")
@router.post("/parse-only/")
@limiter.limit(_RATE_LIMIT)
async def parse_only(
    request: Request,
    file: UploadFile = File(...),
    target_id: str = Form(""),
):
    """
    訪客免登入解析端點。
    - 完整執行 ingest + geocode 流程
    - 回傳 GeoJSON FeatureCollection（與 /upload/parse-temp 格式相同）
    - 不寫入 raw_traces / evidence_files / audit_logs 等任何資料表
    - Rate-limited：同一 IP 每小時 20 次；超過回傳 429
    """
    filename = file.filename or "upload"
    content = await file.read()

    if not target_id:
        target_id = filename.rsplit(".", 1)[0]

    client_ip = request.client.host if request.client else "?"
    print(
        f"[parse-only] ip={client_ip} file={filename} "
        f"target={target_id} size={len(content)}B"
    )

    try:
        records = parse_file_only(target_id, filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"解析失敗：{type(e).__name__}: {e}")

    features = []
    skipped = 0
    for r in records:
        if r.get("lat") is None or r.get("lng") is None:
            skipped += 1
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["lng"], r["lat"]],
                },
                "properties": {
                    "target_id":   r["target_id"],
                    "cell_addr":   r.get("cell_addr"),
                    "start_ts":    r.get("start_ts"),
                    "end_ts":      r.get("end_ts"),
                    "cell_id":     r.get("cell_id"),
                    "accuracy_m":  r.get("accuracy_m"),
                    "azimuth":     r.get("azimuth"),
                    "azimuth_ref": r.get("azimuth_ref", "unknown"),
                    "sector_id":   r.get("sector_id"),
                    "sector_name": r.get("sector_name"),
                    "site_code":   r.get("site_code"),
                },
            }
        )

    return {
        "type":     "FeatureCollection",
        "features": features,
        "total":    len(records),
        "plotted":  len(features),
        "skipped":  skipped,
        "_source":  "parse-only",
        "_records": records,
    }
