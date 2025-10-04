# backend/app/db/session.py
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

DSN = os.getenv("DATABASE_URL", "")
if DSN.startswith("postgres://"):
    DSN = DSN.replace("postgres://", "postgresql://", 1)

def _configure_connection(conn):
    """
    針對每個新連線做一次設定：
    - 關閉 server-side prepared statements（對 Supabase pooler 友善）
    """
    try:
        # psycopg3 的層級參數；把門鎖死
        conn.prepare_threshold = 0
    except Exception:
        pass

# 建立連線池（open=False：由應用啟動時再 open）
pool = ConnectionPool(
    conninfo=DSN,
    min_size=1,
    max_size=5,
    open=False,
    configure=_configure_connection,
    # 若你的 psycopg 版本支援，kwargs 也一併送出（雙重保險）
    kwargs={"prepare_threshold": 0},
)

@contextmanager
def get_conn():
    """
    取得同步連線；用法：
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
    """
    if pool.closed:
        pool.open()
    with pool.connection() as conn:
        yield conn