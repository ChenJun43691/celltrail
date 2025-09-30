# app/api/stats.py
import os
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request
from pydantic import BaseModel

# 以 redis-py 的 asyncio 版本連線
from redis import asyncio as aioredis

router = APIRouter()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

class HitIn(BaseModel):
    project_id: str | None = None

def _today_key():
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def _client_ip(req: Request) -> str:
    # 先看反向代理頭，再退回到連線 IP
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"

@router.post("/stats/hit")
async def hit(req: Request, payload: HitIn | None = None):
    """
    計一次使用（有簡單去重：同一 IP 一小時內只記一次）
    回傳：{ ok, total, today }
    """
    ip = _client_ip(req)
    dedup_key = f"stats:seen:{ip}"
    # setnx + expire 做 1 小時去重
    is_new = await r.setnx(dedup_key, "1")
    if is_new:
        await r.expire(dedup_key, 3600)  # 1 小時

        # 總次數 / 今日次數 +1
        today = _today_key()
        pipe = r.pipeline()
        pipe.incr("stats:total")
        pipe.incr(f"stats:day:{today}")
        total, today_cnt = await pipe.execute()
    else:
        # 已經計過，直接查目前數字
        today = _today_key()
        pipe = r.pipeline()
        pipe.get("stats:total")
        pipe.get(f"stats:day:{today}")
        total, today_cnt = await pipe.execute()
        total = int(total or 0)
        today_cnt = int(today_cnt or 0)

    return {"ok": True, "total": total, "today": today_cnt}

@router.get("/stats")
async def get_stats():
    """
    取目前的總次數 / 今日次數
    """
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