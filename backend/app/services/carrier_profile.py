# backend/app/services/carrier_profile.py
"""
CellTrail Carrier Profile Service
─────────────────────────────────
把「電信業者欄名 → 系統 canonical 欄名」對照表從 ingest.py 內的 _RAW2CANON
常數搬到 DB 表 `carrier_profiles`，並提供讀取 / cache / invalidate 介面。

為什麼要拉成獨立服務（不直接在 ingest.py 內查 DB）：
  1. **單一職責**：ingest.py 處理「檔案 → row dict」；本檔負責「row dict 鍵正規化」
     的 mapping source，兩者互不干擾。
  2. **避免 hot path 反覆查 DB**：每次匯入動輒上萬列，每列都查 DB 會把連線池打爆。
     用 module-level cache，啟動載一次即可。
  3. **W2/W3 鋪路**：未來會多支「依 fingerprint 取對應 profile」「LLM 輔助新增 profile」
     等查詢，全部集中在這裡。

對外介面：
  - get_active_header_map() -> Dict[str, str]
        回傳「正規化過的欄名 (_canon 後)」→「canonical 欄名」對應表。
        ingest._normalize_row 直接拿這個 dict 用，介面與舊 HEADER_MAP 完全一致。
  - get_default_profile() -> dict | None
        回傳整筆 default profile（含 mapping_json 原始鍵 + 稽核欄位）。供管理介面查看。
  - invalidate_cache() -> None
        強制清除 cache；W2/W3 新增/編輯 profile 後呼叫。

DB 連不上時的 fallback：
  本服務會 import ingest 內的 _RAW2CANON 常數作為最終後盾，保證即使資料庫掛了，
  匯入功能仍可運作（功能等價，只是無法新增 profile）。
"""

from __future__ import annotations
import threading
from typing import Dict, Any, Optional

# 注意：app.db.session 的 import 改成 lazy（在 _load_default_profile_from_db 內），
# 為什麼這樣設計：
#   1. db.session 在 module load 時會 import psycopg；若測試環境沒裝 psycopg，
#      本檔的純函式（_canon、_build_header_map_from_mapping）也會無法 import
#   2. service 層的 DB 依賴本就應該收斂在「實際需要 DB 的函式」內，這是更乾淨的設計
#   3. 環境變數 DATABASE_URL 沒設定時 db.session 會在 import 期 raise，
#      lazy 後就只在實際嘗試查 DB 時才會撞到 — 行為更可預測

# ============================================================
# Module-level cache（thread-safe lazy load）
# ============================================================
_LOCK = threading.RLock()
_HEADER_MAP_CACHE: Optional[Dict[str, str]] = None
_DEFAULT_PROFILE_CACHE: Optional[Dict[str, Any]] = None


def _canon(s: Any) -> str:
    """
    與 ingest._canon 邏輯**完全一致**的本地副本。
    為什麼不直接 import ingest._canon？
      - ingest.py 在啟動期會 import 本檔（透過 services 套件 __init__），
        若我們反過來 import ingest 會形成 circular import。
      - 副本只有 ~10 行，維護成本可接受；單元測試會驗證兩者結果一致。
    """
    import re
    if s is None:
        return ""
    s = str(s)
    try:
        import unicodedata
        s = unicodedata.normalize("NFKC", s)
    except Exception:
        pass
    s = s.replace("臺", "台")
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[•·．\.\-:：;；,/，、\t]", "", s)
    return s.lower()


def _load_default_profile_from_db() -> Optional[Dict[str, Any]]:
    """
    從 DB 撈 is_default=TRUE 且 is_active=TRUE 的那一筆 profile。
    回傳 None 表示「DB 內沒有 default profile」（schema 還沒套用 / 種子 INSERT 失敗）。
    DB 連線錯誤會 raise（讓上層決定 fallback 策略）。
    """
    from app.db.session import get_conn  # lazy import：見 module 頂端註解
    sql = """
    SELECT id, carrier_name, variant_label, mapping_json,
           is_default, is_active, notes,
           created_by, approved_by, approved_at,
           llm_assisted, llm_model, llm_prompt_hash,
           created_at, updated_at
      FROM carrier_profiles
     WHERE is_default = TRUE AND is_active = TRUE
     ORDER BY id ASC
     LIMIT 1
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, prepare=False)
        row = cur.fetchone()
        if not row:
            return None
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row))


def _build_header_map_from_mapping(mapping: Dict[str, str]) -> Dict[str, str]:
    """
    {"開始連線時間": "start_ts", ...}（原始鍵）
       ↓ 對 key 做 _canon
    {"開始連線時間": "start_ts", "始話時間": "start_ts", ...}（正規化過的鍵）

    為什麼存進 DB 的是「原始鍵」而不是「正規化鍵」：
      - 可讀性：DBA / 管理員直接看 JSON 能秒懂
      - 可重新計算：_canon 邏輯之後可能微調（例如新增「全角斜線」處理），
        若 DB 存的是 _canon 過的鍵會永遠 stale；存原始鍵則重新 canon 即可。
    """
    return {_canon(k): v for k, v in (mapping or {}).items()}


def _ingest_fallback_map() -> Dict[str, str]:
    """
    DB 不可用時的最終後盾：直接 import ingest._RAW2CANON。
    這個 import 放在 function 內（lazy）以避免 module load 時的 circular import。
    """
    try:
        from app.services.ingest import _RAW2CANON  # type: ignore
        return _build_header_map_from_mapping(_RAW2CANON)
    except Exception:
        # 連 ingest 也壞了 → 回空 dict，至少不 crash。所有列都會被當「沒有已知欄」丟棄。
        return {}


# ============================================================
# Public API
# ============================================================
def get_active_header_map() -> Dict[str, str]:
    """
    取得當前生效的 header map（{canon(原始欄名): canonical 欄名}）。

    呼叫順序：
      1. 若 cache 有 → 直接回
      2. 嘗試從 DB 讀 default profile → 命中則建 map、寫 cache、回
      3. DB 查詢失敗 / 沒有 default profile → fallback 到 ingest._RAW2CANON

    為什麼用 RLock 而非 Lock：
      理論上 _build_header_map_from_mapping 不會回頭呼叫本函式，但為了未來
      若 ingest fallback 路徑變複雜時不會死鎖，採用 reentrant lock 較保險。
    """
    global _HEADER_MAP_CACHE, _DEFAULT_PROFILE_CACHE
    if _HEADER_MAP_CACHE is not None:
        return _HEADER_MAP_CACHE

    with _LOCK:
        # double-check（其他 thread 可能已經填好）
        if _HEADER_MAP_CACHE is not None:
            return _HEADER_MAP_CACHE

        try:
            profile = _load_default_profile_from_db()
        except Exception as e:
            # DB 連不上 / schema 未套用 → fallback；但保留 print 痕跡，運維可發現
            print(f"[carrier_profile] DB 讀取失敗，fallback 到 ingest._RAW2CANON：{type(e).__name__}: {e}")
            _HEADER_MAP_CACHE = _ingest_fallback_map()
            _DEFAULT_PROFILE_CACHE = None
            return _HEADER_MAP_CACHE

        if profile is None:
            print("[carrier_profile] DB 內無 default profile，fallback 到 ingest._RAW2CANON")
            _HEADER_MAP_CACHE = _ingest_fallback_map()
            _DEFAULT_PROFILE_CACHE = None
            return _HEADER_MAP_CACHE

        _DEFAULT_PROFILE_CACHE = profile
        _HEADER_MAP_CACHE = _build_header_map_from_mapping(profile["mapping_json"])
        print(f"[carrier_profile] 已載入 default profile: id={profile['id']} "
              f"variant={profile['variant_label']} 別名數={len(_HEADER_MAP_CACHE)}")
        return _HEADER_MAP_CACHE


def get_default_profile() -> Optional[Dict[str, Any]]:
    """供管理介面 / 報告 / 測試查看。觸發 cache 載入。"""
    get_active_header_map()
    return _DEFAULT_PROFILE_CACHE


def invalidate_cache() -> None:
    """
    清除快取。下一次 get_active_header_map() 會重新從 DB 讀。
    呼叫時機（W2/W3）：
      - 新增 profile 後
      - 修改 default profile 後
      - 切換 default 旗標後
    """
    global _HEADER_MAP_CACHE, _DEFAULT_PROFILE_CACHE
    with _LOCK:
        _HEADER_MAP_CACHE = None
        _DEFAULT_PROFILE_CACHE = None
