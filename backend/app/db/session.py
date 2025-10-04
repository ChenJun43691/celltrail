# backend/app/db/session.py
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL is not set")

MIN_SIZE = int(os.getenv("DB_POOL_MIN", "1"))
MAX_SIZE = int(os.getenv("DB_POOL_MAX", "10"))
CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
APP_NAME = os.getenv("DB_APP_NAME", "celltrail-api")

# 關閉 server-side prepared statements（對 Supabase/pgBouncer 友善）
pool = ConnectionPool(
    conninfo=DB_URL,
    min_size=MIN_SIZE,
    max_size=MAX_SIZE,
    kwargs={
        "prepare_threshold": 0,
        "connect_timeout": CONNECT_TIMEOUT,
        "application_name": APP_NAME,
    },
)

@contextmanager
def get_conn():
    """
    用法：
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    try:
        pool.open()   # 多次 open() 安全
    except Exception:
        pass
    with pool.connection() as conn:
        yield conn