# app/services/limiter.py
"""
集中定義 slowapi Limiter 實例，供各路由模組共用。

slowapi 依賴 fastapi.Request；關鍵字函式從 request.client.host 取 IP。
429 例外由 main.py 的 _rate_limit_handler 統一攔截並回傳繁體中文訊息。
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)


# ── 訪客配額（P9 Phase 2B）────────────────────────────────────
# 為什麼不用 @limiter.limit 裝飾器：slowapi 的 exempt_when 被呼叫時**不帶任何參數**
# （見 slowapi.wrappers.Limit.is_exempt），拿不到 request，也就無從判斷「這一次是不是
# 訪客」。而 POST /api/preview 是登入與訪客共用的同一支端點：對登入偵查員套 20/hr
# 會誤傷正常辦案（一個案件動輒十幾個歷程檔），對訪客不套又等於開放匿名寫入
# preview_artifacts（會落加密原始檔，成本比 parse-only 高）。
#
# 故改為「在 handler 內判定身分後，才手動 hit 同一個 slowapi 後端計數器」：
# 沿用 limiter.limiter（FixedWindowRateLimiter）與同一份 storage，行為與裝飾器一致，
# 只是由我們決定何時計數。key 另加前綴，與 /api/parse-only 的配額**各自獨立**。
from limits import parse as _parse_limit
from limits.strategies import RateLimiter as _RateLimiter  # noqa: F401  (型別說明用)

GUEST_PREVIEW_LIMIT = "20/hour"
_GUEST_PREVIEW_ITEM = _parse_limit(GUEST_PREVIEW_LIMIT)


def hit_guest_preview_quota(request) -> bool:
    """訪客建立 preview 時扣一次配額。回 True=放行、False=已超額。

    storage 故障時**放行**（回 True）：配額是防濫用的次要防線，不該因為
    計數器掛掉就讓整個訪客上傳流程停擺（與 geocode 對 Redis 的處理原則一致）。
    """
    try:
        ip = get_remote_address(request)
        return bool(limiter.limiter.hit(_GUEST_PREVIEW_ITEM, "preview_guest", ip))
    except Exception:
        return True
