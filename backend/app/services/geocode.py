import os, json
from typing import Optional, Tuple

import requests

try:
    import redis  # 可選
except Exception:
    redis = None

REDIS_URL = os.getenv("REDIS_URL")
_r = None
if redis and REDIS_URL:
    try:
        _r = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _r = None

GMAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
GMAPS_ENDPOINT = os.getenv("GOOGLE_GEOCODE_ENDPOINT", "https://maps.googleapis.com/maps/api/geocode/json")
GMAPS_REGION = os.getenv("GOOGLE_REGION", "tw")
GMAPS_LANG = os.getenv("GOOGLE_LANGUAGE", "zh-TW")

CACHE_TTL = 60 * 60 * 24 * 30  # 30 天

def _cache_get(addr: str) -> Optional[Tuple[float, float]]:
    if not _r or not addr:
        return None
    v = _r.get(f"addr:{addr}")
    if not v:
        return None
    try:
        d = json.loads(v)
        return float(d["lat"]), float(d["lng"])
    except Exception:
        return None

def _cache_set(addr: str, lat: float, lng: float) -> None:
    if not _r or not addr:
        return
    try:
        _r.setex(f"addr:{addr}", CACHE_TTL, json.dumps({"lat": lat, "lng": lng}, ensure_ascii=False))
    except Exception:
        pass

def _google_geocode(addr: str) -> Optional[Tuple[float, float]]:
    if not addr or not GMAPS_KEY:
        return None
    params = {
        "address": addr,
        "key": GMAPS_KEY,
        "region": GMAPS_REGION,
        "language": GMAPS_LANG,
    }
    r = requests.get(GMAPS_ENDPOINT, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if j.get("status") == "OK" and j.get("results"):
        loc = j["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])
    return None

def lookup(cell_id: str | None, cell_addr: str | None) -> Optional[Tuple[float, float]]:
    # 以地址為主（你的 Excel 是地址比 cell_id 更完整）
    addr = (cell_addr or "").strip()
    if addr:
        ll = _cache_get(addr)
        if ll:
            return ll
        ll = _google_geocode(addr)
        if ll:
            _cache_set(addr, ll[0], ll[1])
            return ll
    # cell_id 對照可日後擴充（查自建表）
    return None