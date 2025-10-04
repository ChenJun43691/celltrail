# backend/app/db/session.py
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

# Render / Supabase 都會給 DATABASE_URL
DSN = os.getenv("DATABASE_URL", "")
# psycopg3 需要 postgresql://
if DSN.startswith("postgres://"):
    DSN = DSN.replace("postgres://", "postgresql://", 1)

# 關閉 server-side prepared statements，避免經過 pooler 出問題
pool = ConnectionPool(
    conninfo=DSN,
    min_size=1,
    max_size=5,
    kwargs={"prepare_threshold": 0},
    open=False,   # 啟動時再 open
)

@contextmanager
def get_conn():
    """
    取得同步連線；with get_conn() as conn: ...
    """
    if pool.closed:
        try:
            pool.open()
        except Exception:
            # 讓上層決定是否要 fail；此處不吞例外
            raise
    with pool.connection() as conn:
        yield conn