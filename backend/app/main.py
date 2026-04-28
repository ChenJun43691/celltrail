# backend/app/main.py
"""
CellTrail FastAPI 入口。

重點：
- 使用 FastAPI 現代化的 lifespan context manager（取代已 deprecated 的 @app.on_event）。
- CORS 以白名單模式設定，避免 regex 誤傷。
- 所有路由統一掛在 /api 之下（OpenAPI 也掛在 /api/openapi.json）。
"""
from dotenv import load_dotenv
load_dotenv()

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import pool


# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # === startup ===
    print("[CORS] allow_origins =", allow_origins)
    try:
        pool.open()
        pool.wait(10)
        print(f"[DB] pool ready (min={pool.min_size}, max={pool.max_size})")
    except Exception as e:
        print(f"[DB] pool warmup error: {type(e).__name__}: {e}")

    yield

    # === shutdown ===
    # 註：psycopg-pool 3.2.x 的 ConnectionPool.close() 本身即為同步收尾，
    # 不需要（也沒有）wait_close() 這個方法。
    try:
        pool.close()
        print("[DB] pool closed")
    except Exception as e:
        print(f"[DB] pool close error: {type(e).__name__}: {e}")


# ---------- CORS 白名單 ----------
# 為什麼預設要列 5500/5501/5173 三個 dev port：
#   - 5500：VS Code Live Server 預設
#   - 5501：5500 被 Live Server 佔住時的 fallback（python3 -m http.server 5501）
#   - 5173：Vite dev server 預設
# 上線時用 ENV var CORS_ORIGINS 覆蓋；本機若 .env 已設則以 .env 為準。
raw = os.getenv(
    "CORS_ORIGINS",
    ",".join([
        "https://celltrail.netlify.app",
        "http://localhost:5500",  "http://127.0.0.1:5500",
        "http://localhost:5501",  "http://127.0.0.1:5501",
        "http://localhost:5173",  "http://127.0.0.1:5173",
    ]),
)
allow_origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


# ---------- FastAPI App ----------
app = FastAPI(
    title="CellTrail API",
    version="0.2.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Routers ----------
# 放在 app 建好與 middleware 設好之後再 import / include
from app.api.health  import router as health_router  # noqa: E402
from app.api.upload  import router as upload_router   # noqa: E402
from app.api.map     import router as map_router      # noqa: E402
from app.api.targets import router as targets_router  # noqa: E402
from app.api.auth    import router as auth_router     # noqa: E402
from app.api.stats   import router as stats_router    # noqa: E402
from app.api.users   import router as users_router    # noqa: E402
from app.api.geocode import router as geocode_router  # noqa: E402
from app.api.audit   import router as audit_router    # noqa: E402
from app.api.report  import router as report_router   # noqa: E402

app.include_router(health_router,  prefix="/api/health", tags=["health"])
app.include_router(auth_router,    prefix="/api",        tags=["auth"])
app.include_router(users_router,   prefix="/api",        tags=["users"])
app.include_router(upload_router,  prefix="/api/upload", tags=["upload"])
app.include_router(map_router,     prefix="/api",        tags=["map"])
app.include_router(targets_router, prefix="/api",        tags=["targets"])
app.include_router(stats_router,   prefix="/api",        tags=["stats"])
app.include_router(geocode_router, prefix="/api",        tags=["geocode"])
app.include_router(audit_router,   prefix="/api",        tags=["audit"])
app.include_router(report_router,  prefix="/api",        tags=["report"])


@app.get("/api")
def root():
    return {"app": "CellTrail", "version": "0.2.0", "status": "ok"}
