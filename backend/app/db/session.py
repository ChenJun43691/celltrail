# backend/app/db/session.py
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://celltrail:celltrail@localhost:5432/celltrail"
)

# 依主機資源調整 pool 大小；autocommit=True
pool: ConnectionPool = ConnectionPool(
    DB_URL,
    min_size=1,
    max_size=10,
    kwargs={"autocommit": True},
)

@contextmanager
def get_conn():
    """
    從連線池取一條連線，並關閉 psycopg3 的自動 prepared statements，
    避免 DuplicatePreparedStatement: "_pg3_x already exists"。
    """
    with get_conn() as conn:
        try:
            # 0 = 關閉；預設約 5（在多 worker 下容易撞名）
            conn.prepare_threshold = 0
        except Exception:
            # 舊版 psycopg 若沒有此屬性也沒關係
            pass
        yield conn