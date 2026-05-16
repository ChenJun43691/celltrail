# backend/app/services/geocode.py
import os, json, time, re
from typing import Optional, Tuple, List, Dict, Iterable
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
    try:
        v = _r.get(f"addr:{addr}")
        if not v:
            return None
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
    """
    OSM Nominatim 備援，兩段式查詢：
    Pass 1 — 自由格式（q=addr）：對較知名地點有效。
    Pass 2 — 結構化（city + street）：Nominatim 對台灣中文地址的自由格式命中率低，
             但結構化查詢且街道號碼置前（e.g. "211號中正四路"）可大幅提升命中率。
             實測：自由格式 0/3，結構化 1/1（高雄市前金區中正四路211號）。
    """
    if not USE_OSM:
        return None

    def _request(params: dict) -> Optional[Tuple[float, float]]:
        try:
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
                time.sleep(1.0)  # 禮貌性節流（Nominatim 使用政策）
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as e:
            print(f"[geocode] OSM 例外: {type(e).__name__}: {e} addr={addr!r}")
        return None

    base = {"format": "json", "limit": 1, "countrycodes": "tw"}

    # Pass 1: 自由格式
    result = _request({**base, "q": addr})
    if result:
        return result

    # Pass 2: 結構化 — 解析「縣市 + 路名 + 門號」，轉成 Nominatim 偏好的號碼前置格式
    # "高雄市前金區中正四路211號" → city=高雄市, street=211號中正四路
    m = re.match(r"([\S]+?[市縣])([\S]+?[區鄉鎮市])?([\S]+?(?:路|街|大道|巷|弄))(\d+號)?", addr)
    if m:
        city   = m.group(1)
        road   = m.group(3) or ""
        num    = m.group(4) or ""
        street = f"{num}{road}".strip() if num else road
        if city and street:
            result = _request({**base, "city": city, "street": street})
            if result:
                return result

    print(f"[geocode] OSM 查無結果 addr={addr!r}")
    return None

# ---------- cell_id 本地對照（查 cell_towers 表） ----------
def _lookup_from_local(cell_id: Optional[str], addr: Optional[str]) -> Optional[Tuple[float, float]]:
    if not cell_id or not str(cell_id).strip():
        return None
    try:
        from app.db.session import get_conn  # lazy import：避免 test 環境循環依賴
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lat, lng FROM cell_towers WHERE cell_id = %s LIMIT 1",
                    (str(cell_id).strip(),),
                    prepare=False,
                )
                row = cur.fetchone()
                if row:
                    return float(row[0]), float(row[1])
    except Exception as e:
        print(f"[geocode] cell_towers lookup error: {type(e).__name__}: {e}")
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


# ---------- 批次查詢（ingest 大檔上傳用，避免逐筆 round-trip） ----------
def lookup_bulk(
    unique_keys: List[Tuple[Optional[str], Optional[str]]],
) -> Dict[Tuple[Optional[str], Optional[str]], Optional[Tuple[float, float]]]:
    """
    批次解析 (cell_id, cell_addr) → (lat, lng)。
    優化重點：
      1. 本地 cell_towers 一次 SQL `ANY()` 全撈
      2. Redis 一次 MGET 批次（避免 3000+ round-trip）
      3. 剩下 cache miss 才打 Google（這個無法批次，仍序列）

    回傳 dict：key 為原始 (cell_id, cell_addr)，值為 (lat, lng) 或 None。
    """
    import time as _time
    result: Dict[Tuple[Optional[str], Optional[str]], Optional[Tuple[float, float]]] = {}
    if not unique_keys:
        return result

    _t_start = _time.perf_counter()
    n_local_hit = 0
    n_redis_hit = 0
    n_google_call = 0
    n_no_addr = 0

    # ── Step 1: 本地 cell_towers 一次撈 ─────────────────────────
    cell_ids = list({k[0] for k in unique_keys if k[0]})
    local_map: Dict[str, Tuple[float, float]] = {}
    if cell_ids:
        try:
            from app.db.session import get_conn
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT cell_id, lat, lng FROM cell_towers WHERE cell_id = ANY(%s)",
                    (cell_ids,),
                    prepare=False,
                )
                for row in cur.fetchall():
                    local_map[str(row[0])] = (float(row[1]), float(row[2]))
        except Exception as e:
            print(f"[bulk_geocode] local lookup error: {type(e).__name__}: {e}")

    # ── Step 2: 拆分尚未解決的，準備地址清洗 ──────────────────────
    pending: List[Tuple[Tuple[Optional[str], Optional[str]], str]] = []  # (orig_key, simplified)
    addr_keys: List[str] = []                                            # Redis keys for MGET

    for k in unique_keys:
        cell_id, cell_addr = k
        # 先嘗試本地
        if cell_id and str(cell_id) in local_map:
            result[k] = local_map[str(cell_id)]
            n_local_hit += 1
            continue
        # 沒地址、也沒本地對照 → 放棄
        simplified = _simplify_addr(cell_addr or "")
        if not simplified:
            result[k] = None
            n_no_addr += 1
            continue
        pending.append((k, simplified))
        addr_keys.append(f"addr:{simplified}")

    # ── Step 3: Redis 一次 MGET ─────────────────────────────────
    redis_hits: Dict[str, Tuple[float, float]] = {}
    if addr_keys and _r is not None:
        try:
            values = _r.mget(addr_keys)
            for ak, v in zip(addr_keys, values):
                if not v:
                    continue
                try:
                    d = json.loads(v)
                    redis_hits[ak] = (float(d["lat"]), float(d["lng"]))
                except Exception:
                    pass
        except Exception as e:
            print(f"[bulk_geocode] redis mget error: {type(e).__name__}: {e}")

    # ── Step 4: 解析 pending：Redis 有就用、沒有的丟給 Google ────
    miss_for_google: List[Tuple[Tuple[Optional[str], Optional[str]], str]] = []
    for orig_key, simplified in pending:
        ak = f"addr:{simplified}"
        if ak in redis_hits:
            result[orig_key] = redis_hits[ak]
            n_redis_hit += 1
        else:
            miss_for_google.append((orig_key, simplified))

    # ── Step 5: Google（無法批次，序列）+ 寫回 Redis cache ──────
    for orig_key, simplified in miss_for_google:
        ll = _google_geocode(simplified) or _osm_geocode(simplified)
        result[orig_key] = ll
        n_google_call += 1
        if ll:
            _cache_set(simplified, ll[0], ll[1])

    _total = _time.perf_counter() - _t_start
    print(
        f"[bulk_geocode][timing] total={_total*1000:.0f}ms "
        f"unique_keys={len(unique_keys)} "
        f"local_hit={n_local_hit} redis_hit={n_redis_hit} "
        f"google_calls={n_google_call} no_addr={n_no_addr}"
    )
    return result