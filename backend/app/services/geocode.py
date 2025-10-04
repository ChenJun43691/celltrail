# backend/app/services/geocode.py
import os, json, time, re
from typing import Optional, Tuple
import requests

try:
    import redis  # optional
except Exception:
    redis = None

# ---------- Redis cache ----------
REDIS_URL = os.getenv("REDIS_URL")
_r = None
if redis and REDIS_URL:
    try:
        _r = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _r = None

CACHE_TTL = 60 * 60 * 24 * 30  # 30 天

def _cache_get(addr: str) -> Optional[Tuple[float, float]]:
    if not _r:
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
    if not _r:
        return
    try:
        _r.setex(f"addr:{addr}", CACHE_TTL, json.dumps({"lat": lat, "lng": lng}, ensure_ascii=False))
    except Exception:
        pass

# ---------- 地址清洗 ----------
def _simplify_addr(addr: str) -> str:
    if not addr:
        return ""
    s = addr.strip().replace("臺", "台")
    s = re.sub(r"（.*?）|\(.*?\)", "", s)   # 去括號
    s = re.sub(r"號.*$", "號", s)          # 「號」後面全部砍掉（樓層、頂、之x…）
    s = re.sub(r"\s+", "", s)
    return s

# ---------- Google ----------
GMAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
GMAPS_ENDPOINT = os.getenv("GOOGLE_GEOCODE_ENDPOINT", "https://maps.googleapis.com/maps/api/geocode/json")
GMAPS_REGION = os.getenv("GOOGLE_REGION", "tw")
GMAPS_LANG = os.getenv("GOOGLE_LANGUAGE", "zh-TW")

def _google_geocode(addr: str) -> Optional[Tuple[float, float]]:
    if not GMAPS_KEY:
        return None
    try:
        r = requests.get(
            GMAPS_ENDPOINT,
            params={"address": addr, "key": GMAPS_KEY, "region": GMAPS_REGION, "language": GMAPS_LANG},
            timeout=12,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("status") == "OK" and j.get("results"):
            loc = j["results"][0]["geometry"]["location"]
            return float(loc["lat"]), float(loc["lng"])
    except Exception:
        pass
    return None

# ---------- OSM（Nominatim）備援 ----------
USE_OSM = os.getenv("GEO_OSM_FALLBACK") == "1"
OSM_URL = "https://nominatim.openstreetmap.org/search"

def _osm_geocode(addr: str) -> Optional[Tuple[float, float]]:
    if not USE_OSM:
        return None
    try:
        r = requests.get(
            OSM_URL,
            params={"q": addr, "format": "json", "limit": 1},
            headers={"User-Agent": "celltrail/1.0"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            time.sleep(1.0)  # 禮貌性節流
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None

# ---------- 預留：cell_id 對照（之後可接資料表） ----------
def _lookup_from_local(cell_id: Optional[str], addr: Optional[str]) -> Optional[Tuple[float, float]]:
    return None

# ---------- 對外 API ----------
def lookup(cell_id: Optional[str], cell_addr: Optional[str]) -> Optional[Tuple[float, float]]:
    # 1) 本地對照
    ll = _lookup_from_local(cell_id, cell_addr)
    if ll:
        return ll

    # 2) 地址 → 清洗 → 快取 → Google → OSM
    addr = _simplify_addr(cell_addr or "")
    if not addr:
        return None

    ll = _cache_get(addr)
    if ll:
        return ll

    ll = _google_geocode(addr) or _osm_geocode(addr)
    if ll:
        _cache_set(addr, ll[0], ll[1])
    return ll