from fastapi import APIRouter
import os, redis
from psycopg_pool import ConnectionPool

router = APIRouter()

DB_URL = os.getenv("DATABASE_URL", "postgresql://celltrail:celltrail@localhost:5432/celltrail")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# psycopg3 連線池（autocommit=True，方便簡單查詢）
pool = ConnectionPool(DB_URL, min_size=1, max_size=5, kwargs={"autocommit": True})
r = redis.from_url(REDIS_URL, decode_responses=True)

@router.get("/")
def health():
    db_ok, postgis_ok, redis_ok = False, False, False
    db_version, postgis_version = None, None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                db_version = cur.fetchone()[0]
                # PostGIS 若未安裝會噴錯，因此用 try 包起來
                try:
                    cur.execute("SELECT postgis_full_version();")
                    postgis_version = cur.fetchone()[0]
                    postgis_ok = True
                except Exception:
                    postgis_ok = False
        db_ok = True
    except Exception:
        db_ok = False

    try:
        r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return {
        "db_ok": db_ok, "db_version": db_version,
        "postgis_ok": postgis_ok, "postgis_version": postgis_version,
        "redis_ok": redis_ok
    }