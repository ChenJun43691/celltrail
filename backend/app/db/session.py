# backend/app/db/session.py
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://celltrail:celltrail@localhost:5432/celltrail"
)

# 依主機資源調整；autocommit=True
pool: ConnectionPool = ConnectionPool(
    DB_URL,
    min_size=1,
    max_size=10,
    kwargs={"autocommit": True},
)

@contextmanager
def get_conn():
    """
    從連線池取一條連線，並關掉 psycopg3 的 server-side prepared statements。
    這能避免 DuplicatePreparedStatement: "_pg3_x already exists"。
    """
    with get_conn() as conn:
        try:
            # 0 = 關閉；某些版本沒有此屬性，容錯即可
            conn.prepare_threshold = 0
        except Exception:
            pass
        yield conn