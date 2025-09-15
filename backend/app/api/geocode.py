from fastapi import APIRouter, Query, HTTPException
import os, requests, json, re, hashlib
import redis

router = APIRouter()

GOOGLE_KEY      = os.getenv("GOOGLE_MAPS_API_KEY", "")
GEOCODE_URL     = os.getenv("GOOGLE_GEOCODE_ENDPOINT", "https://maps.googleapis.com/maps/api/geocode/json")
GOOGLE_REGION   = os.getenv("GOOGLE_REGION", "tw")
GOOGLE_LANGUAGE = os.getenv("GOOGLE_LANGUAGE", "zh-TW")
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")

rds = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def _norm(addr: str) -> str:
    # 簡單正規化：去前後空白、壓縮空白
    a = addr.strip()
    a = re.sub(r"\s+", "", a)
    return a

@router.get("/geocode")
def geocode(address: str = Query(..., min_length=1, description="完整門牌或地名"),
            use_cache: bool = True):
    if not GOOGLE_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY not configured")

    addr = _norm(address)
    cache_key = "geocode:v1:" + hashlib.sha1((addr + "|" + GOOGLE_REGION).encode("utf-8")).hexdigest()

    if use_cache:
        cached = rds.get(cache_key)
        if cached:
            res = json.loads(cached)
            res["cache"] = "hit"
            return res

    params = {
        "address": address,          # 注意：傳原字串給 Google（不要用 _norm 後的，避免過度簡化）
        "key": GOOGLE_KEY,
        "region": GOOGLE_REGION,
        "language": GOOGLE_LANGUAGE,
    }

    try:
        r = requests.get(GEOCODE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"geocode upstream error: {e}")

    status = data.get("status")
    if status != "OK" or not data.get("results"):
        # ZERO_RESULTS / OVER_QUERY_LIMIT / REQUEST_DENIED / INVALID_REQUEST ...
        raise HTTPException(status_code=404, detail=f"geocode failed: {status}")

    res0 = data["results"][0]
    loc = res0["geometry"]["location"]
    result = {
        "query": address,
        "formatted_address": res0.get("formatted_address"),
        "lat": loc.get("lat"),
        "lng": loc.get("lng"),
        "place_id": res0.get("place_id"),
        "types": res0.get("types", []),
        "partial_match": res0.get("partial_match", False),
    }

    if use_cache:
        rds.setex(cache_key, 7 * 24 * 3600, json.dumps(result))  # 快取 7 天
    result["cache"] = "miss"
    return result