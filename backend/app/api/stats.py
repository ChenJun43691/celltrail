# app/api/stats.py
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from pydantic import BaseModel

from redis import asyncio as aioredis

router = APIRouter()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

class HitIn(BaseModel):
    project_id: str | None = None

def _today_key():
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"

@router.post("/stats/hit")
async def hit(req: Request, payload: HitIn | None = None):
    """
    計一次使用（有簡單去重：同一 IP 一小時內只記一次）
    回傳：{ ok, total, today }

    Redis 不在線時回降級 JSON（不 raise），讓 CORS header 正常跟著 response，
    避免 ServerErrorMiddleware 在 CORSMiddleware 外層攔截 → 500 無 CORS header。
    """
    try:
        ip = _client_ip(req)
        dedup_key = f"stats:seen:{ip}"
        is_new = await r.set(dedup_key, "1", nx=True, ex=3600)
        today = _today_key()
        if is_new:
            pipe = r.pipeline()
            pipe.incr("stats:total")
            pipe.incr(f"stats:day:{today}")
            total, today_cnt = await pipe.execute()
        else:
            pipe = r.pipeline()
            pipe.get("stats:total")
            pipe.get(f"stats:day:{today}")
            total, today_cnt = await pipe.execute()
            total = int(total or 0)
            today_cnt = int(today_cnt or 0)
        return {"ok": True, "total": total, "today": today_cnt}
    except Exception:
        return {"ok": True, "total": 0, "today": 0}


@router.get("/stats")
async def get_stats():
    """
    取目前的總次數 / 今日次數。Redis 不在線時回降級 JSON。
    """
    try:
        today = _today_key()
        pipe = r.pipeline()
        pipe.get("stats:total")
        pipe.get(f"stats:day:{today}")
        total, today_cnt = await pipe.execute()
        return {
            "ok": True,
            "total": int(total or 0),
            "today": int(today_cnt or 0),
            "date": today,
        }
    except Exception:
        return {"ok": True, "total": 0, "today": 0, "date": _today_key()}
