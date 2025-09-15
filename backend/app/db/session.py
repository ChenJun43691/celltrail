import os
from psycopg_pool import ConnectionPool

DB_URL = os.environ.get("DATABASE_URL", "postgresql://celltrail:celltrail@localhost:5432/celltrail")

pool: ConnectionPool = ConnectionPool(DB_URL, min_size=1, max_size=10, kwargs={"autocommit": True})
