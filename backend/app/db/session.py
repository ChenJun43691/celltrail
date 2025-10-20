# backend/app/db/session.py
import os
from contextlib import contextmanager
import psycopg
from psycopg_pool import ConnectionPool

# 1) 取得 DSN（DATABASE_URL 或 SUPABASE_DB_URL）
DSN = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or ""
if not DSN:
    raise RuntimeError("DATABASE_URL / SUPABASE_DB_URL 未設定")

# 2) 某些平台給的是 postgres://，要改為 postgresql://
if DSN.startswith("postgres://"):
    DSN = DSN.replace("postgres://", "postgresql://", 1)

# 3) 連線初始化：關閉 server-side prepared statements、開 autocommit
def _configure_connection(conn: psycopg.Connection):
    try:
        conn.prepare_threshold = 0     # ← 最關鍵：0 = 永不使用 prepared
        conn.autocommit = True         # 寫入/讀取都省去顯式 commit
    except Exception:
        pass

# 4) 連線池
pool = ConnectionPool(
    conninfo=DSN,
    min_size=1,
    max_size=int(os.getenv("DB_POOL_MAX", "5")),
    max_idle=60,
    open=False,                       # 第一次取用時才開啟
    configure=_configure_connection,
)

@contextmanager
def get_conn():
    """
    用法：
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1", prepare=False)
    """
    if pool.closed:
        pool.open()
    with pool.connection() as conn:
        yield conn