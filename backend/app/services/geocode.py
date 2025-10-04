# backend/app/services/geocode.py
from __future__ import annotations

import os, json, time, re, unicodedata
from typing import Optional, Tuple

import requests

try:
    import redis  # optional
except Exception:
    redis = None

# =========================
# Redis cache（可選）
# =========================
REDIS_URL = os.getenv("REDIS_URL")
_r = None
if redis and REDIS_URL:
    try:
        _r = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _r = None

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

# =========================
# 字串標準化 / 台灣地址清洗
# =========================
_TWN_LEVEL_WORDS = r"(?:省|縣|市|鄉|鎮|區|村|里)"
_STREET_WORDS   = r"(?:路|街|大道|巷|弄)"
_NUM_WORDS      = r"(?:號)"
# 範例：屏東縣東港鎮新生三路175號4樓頂 → 屏東縣東港鎮新生三路175號
#     ：屏東縣東港鎮大潭新段208地號     → 屏東縣東港鎮大潭新段
def _canon(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip()
    s = s.replace("臺", "台")
    s = re.sub(r"\s+", "", s)
    return s

def _strip_floor_and_after(s: str) -> str:
    # 去掉「號」之後的樓層、頂、之x…等
    return re.sub(rf"{_NUM_WORDS}.*$", "號", s)

def _strip_land_parcel(s: str) -> str:
    # 去掉「地號」及其前導數字（常見：xx段208地號）
    s = re.sub(r"地號.*$", "", s)
    # 有些只有段名沒有街名，讓它先回到鄉鎮層級（仍可落在鄉鎮中心）
    return s

def _strip_village(s: str) -> str:
    # 去掉「某某里/村」（地理意義小，常干擾）
    return re.sub(rf"{_TWN_LEVEL_WORDS}[^{_TWN_LEVEL_WORDS}]*?里", "", s)

def _tw_addr_variants(raw: str) -> list[str]:
    """
    產生多個從精確到粗略的查詢版本，逐一嘗試：
    1) 完整清洗版（號後截斷 / 去地號 / 去里）
    2) 去號，只查到『路/街』層級
    3) 只留到『鄉鎮市區』層級（最後退而求其次）
    """
    a = _canon(raw)
    if not a:
        return []

    # 完整清洗：去樓層/之x，去地號，去里
    v1 = _strip_village(_strip_land_parcel(_strip_floor_and_after(a)))

    # 去號（號與其後皆移除，只留路名）
    v2 = re.sub(rf"{_NUM_WORDS}.*$", "", v1)

    # 只留到鄉鎮市區
    m = re.search(rf"^(.*?(?:縣|市|鄉|鎮|區))", v1)
    v3 = m.group(1) if m else v2

    # 去掉尾端多餘標點
    variants = []
    for x in (v1, v2, v3):
        x = re.sub(r"[，。、,.]+$", "", x)
        if x and x not in variants:
            variants.append(x)
    return variants

# =========================
# Google Geocoding（若有金鑰）
# =========================
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

# =========================
# OSM / Nominatim（備援）
# =========================
USE_OSM = os.getenv("GEO_OSM_FALLBACK", "0") == "1"
OSM_URL = "https://nominatim.openstreetmap.org/search"
OSM_EMAIL = os.getenv("NOMINATIM_EMAIL")  # 建議填：聯絡 email 以符合使用政策

def _osm_geocode(addr: str) -> Optional[Tuple[float, float]]:
    if not USE_OSM:
        return None
    try:
        params = {
            "q": addr,
            "format": "json",
            "limit": 1,
            "addressdetails": 0,
            "countrycodes": "tw",
            "dedupe": 1,
        }
        if OSM_EMAIL:
            params["email"] = OSM_EMAIL
        r = requests.get(
            OSM_URL,
            params=params,
            headers={"User-Agent": "celltrail/1.0 (+https://celltrail.netlify.app)"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            # 禮貌性節流，避免 429
            time.sleep(1.0)
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None

# =========================
# 預留：以 cell_id 對照本地表（未實作）
# =========================
def _lookup_from_local(cell_id: Optional[str], addr: Optional[str]) -> Optional[Tuple[float, float]]:
    return None

# =========================
# 對外：lookup
# =========================
def lookup(cell_id: Optional[str], cell_addr: Optional[str]) -> Optional[Tuple[float, float]]:
    """
    回傳 (lat, lng) 或 None
    先本地對照 → 快取 → Google → OSM；地址會從精確逐步退化查詢
    """
    # 1) 本地對照
    ll = _lookup_from_local(cell_id, cell_addr)
    if ll:
        return ll

    addr_raw = (cell_addr or "").strip()
    if not addr_raw:
        return None

    # 2) 快取（以最完整清洗版 v1 當 key）
    variants = _tw_addr_variants(addr_raw)
    if not variants:
        return None
    cache_key = variants[0]
    ll = _cache_get(cache_key)
    if ll:
        return ll

    # 3) 逐一嘗試（Google → OSM），從精確到粗略
    for a in variants:
        ll = _google_geocode(a)
        if ll:
            _cache_set(cache_key, ll[0], ll[1])
            return ll
        ll = _osm_geocode(a)
        if ll:
            _cache_set(cache_key, ll[0], ll[1])
            return ll

    return None