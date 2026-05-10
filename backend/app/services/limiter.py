# app/services/limiter.py
"""
集中定義 slowapi Limiter 實例，供各路由模組共用。

slowapi 依賴 fastapi.Request；關鍵字函式從 request.client.host 取 IP。
429 例外由 main.py 的 _rate_limit_handler 統一攔截並回傳繁體中文訊息。
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
