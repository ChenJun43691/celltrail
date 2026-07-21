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

# ---------- SQL 持久快取（Supabase 無 Redis 時的跨請求快取） ----------
# 為什麼：雲端移除 Redis 後 geocode 結果不跨請求保存 → 每次大檔上傳都重打 Google，
# 易超過 Render 120s 請求上限回 502。SQL 快取讓結果持久化：首次上傳分批灌、之後
# （含逾時重傳）跳過已快取者 → 漸進變快、最終必然成功。
# 表用 CREATE TABLE IF NOT EXISTS 自動建立（冪等），不需手動在 Supabase 跑 migration。
# 所有 cur.execute 帶 prepare=False（pooler 不支援 server-side prepared statements）。
_sql_cache_ready = False

def _ensure_sql_cache() -> bool:
    global _sql_cache_ready
    if _sql_cache_ready:
        return True
    try:
        from app.db.session import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS geocode_cache ("
                "  addr TEXT PRIMARY KEY,"
                "  lat DOUBLE PRECISION NOT NULL,"
                "  lng DOUBLE PRECISION NOT NULL,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
                prepare=False,
            )
            conn.commit()
        _sql_cache_ready = True
    except Exception as e:
        print(f"[geocode] ensure sql cache table failed: {type(e).__name__}: {e}")
    return _sql_cache_ready

def _sql_cache_get_bulk(addrs: List[str]) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    if not addrs or not _ensure_sql_cache():
        return out
    try:
        from app.db.session import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT addr, lat, lng FROM geocode_cache WHERE addr = ANY(%s)",
                (list(addrs),),
                prepare=False,
            )
            for r in cur.fetchall():
                out[str(r[0])] = (float(r[1]), float(r[2]))
    except Exception as e:
        print(f"[geocode] sql cache get error: {type(e).__name__}: {e}")
    return out

def _sql_cache_set_bulk(items: List[Tuple[str, float, float]]) -> None:
    if not items or not _ensure_sql_cache():
        return
    try:
        from app.db.session import get_conn
        # 單一多列 INSERT（一次 round-trip），ON CONFLICT 冪等
        placeholders = ",".join(["(%s,%s,%s)"] * len(items))
        args: List[object] = []
        for a, lat, lng in items:
            args.extend([a, lat, lng])
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO geocode_cache(addr,lat,lng) VALUES "
                + placeholders
                + " ON CONFLICT(addr) DO NOTHING",
                args,
                prepare=False,
            )
            conn.commit()
    except Exception as e:
        print(f"[geocode] sql cache set error: {type(e).__name__}: {e}")

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
# bulk geocode 對「未命中快取的唯一地址」並行打 Google 的併發數。
# Google Geocoding 預設配額約 50 QPS；10 個 worker × 每個約 4 req/s ≈ 40 QPS，留安全邊際。
# 為什麼要並行：雲端無 Redis 快取 + cell_towers 空時，大檔（如 test3 ~2400 唯一地址）
# 序列逐筆打 Google 累積 >110s → 超過 Render 請求上限 502。並行把它壓進請求窗口。
GEO_GOOGLE_CONCURRENCY = max(1, int(os.getenv("GEO_GOOGLE_CONCURRENCY", "10") or "10"))


def _google_enabled() -> bool:
    """Google geocode 硬性開關（**call-time** 讀 env，避免 module-level 常數造成 monkeypatch/
    runtime 不一致）。`GEO_GOOGLE_ENABLED` ∈ {0,false,no,off}（不分大小寫、去前後空白）→ 關閉；
    其餘值與未設定 → 啟用（向後相容）。關閉時保證不建立任何 Google HTTP request。"""
    return os.getenv("GEO_GOOGLE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _google_geocode(addr: str) -> Optional[Tuple[float, float]]:
    """
    打 Google Maps Geocoding API。失敗一律回 None（不 raise），
    但會把原因 print 到 stdout（uvicorn 終端可見），方便排查：
      - Google disabled（GEO_GOOGLE_ENABLED=0）/ API key 未設定 / 無效
      - status != OK（REQUEST_DENIED、OVER_QUERY_LIMIT、ZERO_RESULTS ...）
      - HTTP / 網路 / JSON 例外
    """
    if not _google_enabled():
        # 硬止血：GEO_GOOGLE_ENABLED 關閉 → 在建立任何 requests 呼叫之前即回傳（靜默，無錯誤 log）
        return None
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
                # 6s（原 15s）：OSM 是序列備援，逾時太長會讓「Google 查不到」的尾巴
                # 累積爆掉請求窗口（雲端 502 的元兇之一）。縮短以限制單筆最壞耗時。
                timeout=6,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as e:
            print(f"[geocode] OSM 例外: {type(e).__name__}: {e} addr={addr!r}")
        finally:
            # 節流必須對「每一次送出的請求」生效（2026-07-21 修）。
            # 原本 sleep 寫在 `if data:` 內 → **只有命中才節流**，而未命中是多數
            # 情況（台灣門牌在 OSM 覆蓋稀疏），等於請求連發、直接違反 Nominatim
            # 的 1 req/s 政策。實測後果：大量 429 Too many requests，這些地址被
            # 記成「查無結果」→ 命中率被系統性低估，且重試風暴拖慢整批查詢。
            # 也就是說，這個 bug 同時製造了「OSM 沒用」與「OSM 很慢」兩個假象。
            time.sleep(1.0)
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
    # SQL 持久快取（無 Redis 時的後盾）
    sql = _sql_cache_get_bulk([addr])
    if addr in sql:
        _cache_set(addr, *sql[addr])
        return sql[addr]

    ll = _google_geocode(addr) or _osm_geocode(addr)
    if ll:
        _cache_set(addr, ll[0], ll[1])
        _sql_cache_set_bulk([(addr, ll[0], ll[1])])
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
    優化重點（查詢順序，逐層收斂）：
      1. 本地 cell_towers 一次 SQL `ANY()` 全撈
      2. Redis 一次 MGET 批次（雲端通常無 Redis，視為選配）
      3. SQL geocode_cache 一次 `ANY()` 批次讀（跨請求持久快取）
      4. 仍未命中 → 並行打 Google（ThreadPool），失敗者序列 OSM 備援
      5. 成功結果增量寫回 SQL 快取（逾時被切也保住已完成的）

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

    # ── Step 4: 解析 pending：Redis 有就用、沒有的進下一關 ────
    after_redis: List[Tuple[Tuple[Optional[str], Optional[str]], str]] = []
    for orig_key, simplified in pending:
        ak = f"addr:{simplified}"
        if ak in redis_hits:
            result[orig_key] = redis_hits[ak]
            n_redis_hit += 1
        else:
            after_redis.append((orig_key, simplified))

    # ── Step 4.5: SQL 持久快取批次讀（取代失效的雲端 Redis）──────
    n_sql_hit = 0
    miss_for_google: List[Tuple[Tuple[Optional[str], Optional[str]], str]] = []
    if after_redis:
        sql_hits = _sql_cache_get_bulk(list({s for _, s in after_redis}))
        for orig_key, simplified in after_redis:
            if simplified in sql_hits:
                result[orig_key] = sql_hits[simplified]
                n_sql_hit += 1
                # 順手回填 Redis（若有），下次更快
                _cache_set(simplified, *sql_hits[simplified])
            else:
                miss_for_google.append((orig_key, simplified))

    # ── Step 5: 未命中 → Google（並行）→ 仍失敗者 OSM（序列）+ 寫回 cache ──
    # 去重：不同 orig_key 可能清洗成同一 simplified（同址不同 cell_id）→ 只打一次。
    uniq_simplified = list({s for _, s in miss_for_google})
    geo_map: Dict[str, Optional[Tuple[float, float]]] = {}
    n_osm_call = 0

    if uniq_simplified:
        import concurrent.futures as _cf

        # 增量持久化：每累積 N 筆成功就寫一次 SQL 快取。為什麼不留到最後一次寫——
        # 若 Render 在 120s 把請求切斷（大檔仍可能），最後的單次寫永遠跑不到 →
        # 整批白做、重傳從零開始。增量 flush 讓「已完成的地址」確實落地，重傳跳過、
        # 漸進收斂至成功。
        _FLUSH_EVERY = 100
        pending_writes: List[Tuple[str, float, float]] = []

        def _record(s: str, ll: Optional[Tuple[float, float]]):
            geo_map[s] = ll
            if ll:
                _cache_set(s, ll[0], ll[1])
                pending_writes.append((s, ll[0], ll[1]))
                if len(pending_writes) >= _FLUSH_EVERY:
                    _sql_cache_set_bulk(pending_writes)
                    pending_writes.clear()

        # Google：I/O bound，並行大幅縮短總時間（thread-safe：requests + 唯讀 GMAPS_KEY）。
        # **僅在 GEO_GOOGLE_ENABLED 啟用時執行**；關閉時完全跳過 ThreadPool、不提交任何
        # _google_geocode task → 硬止血、零 Google 請求（n_google_call 維持 0）。
        if _google_enabled():
            workers = min(GEO_GOOGLE_CONCURRENCY, len(uniq_simplified))
            with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_google_geocode, s): s for s in uniq_simplified}
                for fut in _cf.as_completed(futs):
                    s = futs[fut]
                    try:
                        ll = fut.result()
                    except Exception as e:
                        print(f"[bulk_geocode] google parallel error: {type(e).__name__}: {e} addr={s!r}")
                        ll = None
                    _record(s, ll)
            n_google_call = len(uniq_simplified)

        # 尚未解出者（Google 關閉時=全部；啟用時=Google 失敗者）→ OSM 備援。**刻意序列**：
        # Nominatim 政策 1 req/s（_osm_geocode 內含 sleep），並行會違規且可能被 429 封。
        osm_targets = [s for s in uniq_simplified if geo_map.get(s) is None]
        for s in osm_targets:
            _record(s, _osm_geocode(s))
        n_osm_call = len(osm_targets)

        if pending_writes:
            _sql_cache_set_bulk(pending_writes)

    # 對應回所有 orig_key（含去重前的重複）
    for orig_key, simplified in miss_for_google:
        result[orig_key] = geo_map.get(simplified)

    _total = _time.perf_counter() - _t_start
    print(
        f"[bulk_geocode][timing] total={_total*1000:.0f}ms "
        f"unique_keys={len(unique_keys)} "
        f"local_hit={n_local_hit} redis_hit={n_redis_hit} sql_hit={n_sql_hit} "
        f"google_calls={n_google_call} osm_calls={n_osm_call} "
        f"google_workers={GEO_GOOGLE_CONCURRENCY} no_addr={n_no_addr}"
    )
    return result