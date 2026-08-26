"""
地址 → 座標端點（供前端「📍 自訂標記」的「定位 / 新增標記」使用）。

GET /api/geocode?address=...

2026-08-27 重寫 —— 原本這裡是**第五條獨立的地理編碼實作**，直接打 Google：

  ① 它不走 `services/geocode.py`，因此拿不到 `cell_towers` / Redis / SQL 快取 /
     TGOS / OSM 任何一層 —— 明明系統裡已經有幾千筆座標，這支卻一律重新問 Google。
  ② **它不理會 `GEO_GOOGLE_ENABLED`**。那個開關是 2026-07-03 為了止住費用而加的
     「硬止血」，但止不到這裡：金鑰一旦有效，這支就會照樣花錢。
  ③ 它**不需要登入也沒有速率限制**，而前端的自訂標記面板訪客看得到 ——
     等於對外開放一支以本專案帳單支付的 Google 地理編碼代理。
  ④ 金鑰失效時回 `geocode failed: REQUEST_DENIED`，前端原樣顯示成
     「定位失敗：geocode failed: REQUEST_DENIED」—— 使用者無從得知要做什麼。

改為**委派給 `services.geocode.lookup()`**，與上傳/解析走同一條查詢鏈
（cell_towers → Redis → SQL 快取 → TGOS → Google → OSM），於是上述四點一次解決：
快取共用、開關生效、錯誤訊息可行動。速率限制另外加（見下）。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import geocode as geo
from app.services.limiter import limiter

router = APIRouter()

# 為什麼要限流：本端點**無需登入**（前端自訂標記面板訪客也看得到），而它下游可能是
# 計費的 Google API。沒有限流的話，任何人都能拿它當免費代理、帳單記在本專案頭上。
# 60/hr 對正常使用綽綽有餘（人工一個一個標地點），對自動化濫用則不夠用。
_RATE_LIMIT = "60/hour"


@router.get("/geocode")
@limiter.limit(_RATE_LIMIT)
def geocode(
    request: Request,
    address: str = Query(..., min_length=1, description="完整門牌或地名"),
):
    """把地址轉成座標。回應保留 `lat` / `lng` / `formatted_address` 供前端沿用。

    查無結果時回 404，且訊息會說明**為什麼**查不到（哪些來源被停用），
    而不是把上游的 `REQUEST_DENIED` 原樣丟給使用者。
    """
    addr = (address or "").strip()
    if not addr:
        raise HTTPException(status_code=400, detail="請輸入地址")

    # cell_id 傳 None：這是純地址查詢，沒有基地台編號可比對。
    hit: Optional[tuple] = geo.lookup(None, addr)
    if hit:
        lat, lng = hit
        return {
            "query": address,
            "formatted_address": addr,   # 本鏈路不回正規化地址，回填輸入值供前端顯示
            "lat": lat,
            "lng": lng,
        }

    # 查不到時，把「目前哪幾條路是通的」講清楚 —— 否則使用者只會看到「定位失敗」，
    # 而真正的原因（三個來源全被停用）在畫面上完全沒有線索。
    avail = []
    if geo._tgos_enabled():
        avail.append("TGOS")
    if geo._google_enabled():
        avail.append("Google")
    if geo.USE_OSM:
        avail.append("OSM")

    if not avail:
        detail = (
            "查無此地址的座標。目前系統的地址定位來源全數停用"
            "（Google / OSM / TGOS），僅能查詢已匯入基地台座標表的地點。"
            "如需以地址定位，請聯絡管理員啟用地理編碼來源。"
        )
    else:
        detail = f"查無此地址的座標（已嘗試：{'、'.join(avail)}）。請確認地址是否完整。"
    raise HTTPException(status_code=404, detail=detail)
