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

# 關閉 server-side prepared statements（對 Supabase pgBouncer 友善）
# 並設定連線逾時與 application_name
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
    取一條連線；caller 負責建立 cursor 與交易（或用 autocommit）。
    使用方式：
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    # 若尚未 open，這裡補一手（多次 open() 安全）
    try:
        pool.open()
    except Exception:
        pass

    with pool.connection() as conn:
        yield conn