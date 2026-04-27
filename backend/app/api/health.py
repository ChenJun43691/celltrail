# backend/app/api/health.py
"""
Health check endpoint.

回報後端各外部相依（PostgreSQL / PostGIS / Redis）是否可用。
此端點不需登入，可用於 load balancer / uptime monitor。
"""
import os
import redis

from fastapi import APIRouter

from app.db.session import get_conn  # 統一用連線池，不再自建 pool

router = APIRouter()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
_redis = redis.from_url(REDIS_URL, decode_responses=True)


@router.get("/")
def health():
    db_ok = False
    postgis_ok = False
    redis_ok = False
    db_version: str | None = None
    postgis_version: str | None = None

    # --- Postgres ---
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT version();", prepare=False)
            row = cur.fetchone()
            db_version = row[0] if row else None
            db_ok = True

            # PostGIS 可能未安裝，獨立判斷
            try:
                cur.execute("SELECT postgis_full_version();", prepare=False)
                row = cur.fetchone()
                postgis_version = row[0] if row else None
                postgis_ok = bool(postgis_version)
            except Exception:
                postgis_ok = False
    except Exception:
        db_ok = False

    # --- Redis ---
    try:
        _redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return {
        "db_ok": db_ok,
        "db_version": db_version,
        "postgis_ok": postgis_ok,
        "postgis_version": postgis_version,
        "redis_ok": redis_ok,
    }
