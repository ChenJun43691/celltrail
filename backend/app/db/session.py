import os
from psycopg_pool import ConnectionPool

# Primary database URL
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://celltrail:celltrail@localhost:5432/celltrail",
)

# Configure each new connection from the pool
def _configure(conn):
    """
    Per-connection setup:
    - Disable server-side prepared statements to avoid
      DuplicatePreparedStatement errors when pooling / behind proxies.
    - Keep autocommit behaviour aligned with the existing code.
    """
    try:
        # psycopg3: 0 disables automatic server-side PREPARE
        conn.prepare_threshold = 0
    except Exception:
        # Older libpq / environments may not expose the attribute
        pass

    try:
        conn.autocommit = True
    except Exception:
        pass

pool: ConnectionPool = ConnectionPool(
    DB_URL,
    min_size=int(os.getenv("PGPOOL_MIN_SIZE", "1")),
    max_size=int(os.getenv("PGPOOL_MAX_SIZE", "10")),
    configure=_configure,   # ensure every pooled connection is configured
)