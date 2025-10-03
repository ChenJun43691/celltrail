# backend/app/db/session.py
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://celltrail:celltrail@localhost:5432/celltrail"
)

# autocommit=True；連線池大小依主機資源調整
pool: ConnectionPool = ConnectionPool(
    DB_URL,
    min_size=1,
    max_size=10,
    kwargs={"autocommit": True},
)

@contextmanager
def get_conn():
    """
    從連線池取一條連線，並關掉 psycopg3 的 server-side prepared statements，
    避免 DuplicatePreparedStatement: "_pg3_x already exists"
    """
    with pool.connection() as conn:
        try:
            conn.prepare_threshold = 0  # 關閉 prepared statements
        except Exception:
            pass
        yield conn