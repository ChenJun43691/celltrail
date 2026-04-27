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

# ---------- 地址清洗 ----------
def _simplify_addr(addr: str) -> str:
    if not addr:
        return ""
    s = addr.strip().replace("臺", "台")
    s = re.sub(r"（.*?）|\(.*?\)", "", s)  # 去括號註記
    s = re.sub(r"號.*$", "號", s)         # 「號」後面常是樓層/頂/之N，砍掉
    s = re.sub(r"\s+", "", s)
    return s

# ---------- Google ----------
GMAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
GMAPS_ENDPOINT = os.getenv("GOOGLE_GEOCODE_ENDPOINT", "https://maps.googleapis.com/maps/api/geocode/json")
GMAPS_REGION = os.getenv("GOOGLE_REGION", "tw")
GMAPS_LANG = os.getenv("GOOGLE_LANGUAGE", "zh-TW")

def _google_geocode(addr: str) -> Optional[Tuple[float, float]]:
    """
    打 Google Maps Geocoding API。失敗一律回 None（不 raise），
    但會把原因 print 到 stdout（uvicorn 終端可見），方便排查：
      - API key 未設定 / 無效
      - status != OK（REQUEST_DENIED、OVER_QUERY_LIMIT、ZERO_RESULTS ...）
      - HTTP / 網路 / JSON 例外
    """
    if not GMAPS_KEY:
        print("[geocode] GOOGLE_MAPS_API_KEY 未設定，跳過 Google geocode")
        return None
    try:
        r = requests.get(
            GMAPS_ENDPOINT,
            params={"address": addr, "key": GMAPS_KEY, "region": GMAPS_REGION, "language": GMAPS_LANG},
            timeout=12,
        )
        r.raise_for_status()
        j = r.json()
        status = j.get("status")
        if status == "OK" and j.get("results"):
            loc = j["results"][0]["geometry"]["location"]
            return float(loc["lat"]), float(loc["lng"])
        # 非 OK：印出 status + error_message，幫助判斷是 key 問題、配額問題還是查無結果
        print(
            f"[geocode] Google 非 OK: status={status!r} "
            f"error_message={j.get('error_message')!r} addr={addr!r}"
        )
    except Exception as e:
        print(f"[geocode] Google 例外: {type(e).__name__}: {e} addr={addr!r}")
    return None

# ---------- OSM（Nominatim）備援 ----------
USE_OSM = os.getenv("GEO_OSM_FALLBACK") == "1"
OSM_URL = "https://nominatim.openstreetmap.org/search"
OSM_EMAIL = os.getenv("NOMINATIM_EMAIL", "")

def _osm_geocode(addr: str) -> Optional[Tuple[float, float]]:
    """OSM Nominatim 備援。同樣：失敗仍回 None，但把原因 print 出來。"""
    if not USE_OSM:
        return None
    try:
        params = {"q": addr, "format": "json", "limit": 1}
        if OSM_EMAIL:
            params["email"] = OSM_EMAIL
        r = requests.get(
            OSM_URL,
            params=params,
            headers={"User-Agent": "celltrail/1.0", "Accept-Language": GMAPS_LANG},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            time.sleep(1.0)  # 禮貌性節流
            return float(data[0]["lat"]), float(data[0]["lon"])
        print(f"[geocode] OSM 查無結果 addr={addr!r}")
    except Exception as e:
        print(f"[geocode] OSM 例外: {type(e).__name__}: {e} addr={addr!r}")
    return None

# ---------- 預留：cell_id 對照（之後可接資料表） ----------
def _lookup_from_local(cell_id: Optional[str], addr: Optional[str]) -> Optional[Tuple[float, float]]:
    return None

# ---------- 對外 API ----------
def lookup(cell_id: Optional[str], cell_addr: Optional[str]) -> Optional[Tuple[float, float]]:
    """
    查詢順序：本地對照 → 地址清洗 → Redis 快取 → Google → OSM 備援
    失敗（無法定位）回 None；上層（ingest）可依此決定要不要跳過該筆。

    排查盲點小抄：
      - 同一個地址如果先前被快取為失敗，這裡不會重試（目前快取只存成功值）
      - 若 Google 一直回 REQUEST_DENIED，通常是 API key 未啟用 Geocoding API
        或設了 Referer/IP 限制，需到 GCP Console → APIs & Services → Credentials 檢查
      - OSM 備援預設關閉（GEO_OSM_FALLBACK=1 才會啟用）
    """
    # 1) 本地對照
    ll = _lookup_from_local(cell_id, cell_addr)
    if ll:
        return ll

    # 2) 地址 → 清洗 → 快取 → Google → OSM
    addr = _simplify_addr(cell_addr or "")
    if not addr:
        # 沒地址、也沒本地對照資料，就只能放棄
        if cell_addr:
            print(f"[geocode] 地址清洗後為空，放棄：raw={cell_addr!r}")
        return None

    ll = _cache_get(addr)
    if ll:
        return ll

    ll = _google_geocode(addr) or _osm_geocode(addr)
    if ll:
        _cache_set(addr, ll[0], ll[1])
    else:
        # 兩個來源都敗：印一條彙總訊息，對照前面 Google/OSM 個別的錯誤
        print(f"[geocode] 所有來源均無結果 addr={addr!r}")
    return ll