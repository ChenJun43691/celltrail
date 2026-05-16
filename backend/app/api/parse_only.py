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
import json
import time
import traceback

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.services.ingest import parse_file_only, ParseDiagnosisError
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
    mapping: str = Form("", description="使用者手動欄位對應 JSON：{raw_column_name: system_field}"),
):
    """
    訪客免登入解析端點。
    - 完整執行 ingest + geocode 流程
    - 回傳 GeoJSON FeatureCollection（與 /upload/parse-temp 格式相同）
    - 不寫入 raw_traces / evidence_files / audit_logs 等任何資料表
    - Rate-limited：同一 IP 每小時 20 次；超過回傳 429
    """
    filename = file.filename or "upload"
    t0 = time.perf_counter()
    content = await file.read()
    t_read = time.perf_counter() - t0
    print(f"[parse-only][timing] file_read={t_read*1000:.0f}ms size={len(content)}B file={filename}")

    if not target_id:
        target_id = filename.rsplit(".", 1)[0]

    client_ip = request.client.host if request.client else "?"
    print(
        f"[parse-only] ip={client_ip} file={filename} "
        f"target={target_id} size={len(content)}B"
    )

    # 解析使用者 mapping（若有）
    user_mapping = None
    if mapping:
        try:
            user_mapping = json.loads(mapping)
            if not isinstance(user_mapping, dict):
                raise ValueError("mapping 必須是 JSON 物件")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"mapping JSON 格式錯誤：{e}")

    try:
        t1 = time.perf_counter()
        records = parse_file_only(target_id, filename, content, mapping=user_mapping)
        t_parse = time.perf_counter() - t1
        print(f"[parse-only][timing] parse_total={t_parse*1000:.0f}ms records={len(records)} file={filename}")
    except ParseDiagnosisError as e:
        # 智慧診斷：回 422 + diagnosis 結構，前端展示「無法解析」UI
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": "format_unknown",
                "detail": str(e),
                "diagnosis": e.diagnosis,
                "filename": filename,
            },
        )
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
