# backend/app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import pool

# ---- 先建立 app ----
app = FastAPI(
    title="CellTrail API",
    version="0.1.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
)

# ---- CORS：用白名單，避免 regex 誤傷 ----
raw = os.getenv(
    "CORS_ORIGINS",
    "https://celltrail.netlify.app,http://localhost:5500,http://127.0.0.1:5500"
)
allow_origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 啟動/關閉事件（僅各一組）----
@app.on_event("startup")
async def on_startup():
    print("[CORS] allow_origins =", allow_origins)
    # 預熱連線池，避免第一個請求卡住
    try:
        pool.open()
        pool.wait(10)
        print(f"[DB] pool ready (min={pool.min_size}, max={pool.max_size})")
    except Exception as e:
        print(f"[DB] pool warmup error: {type(e).__name__}: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        pool.close()
        # 盡量把執行緒收乾淨，避免 Render 關機時看到 couldn't stop thread
        try:
            pool.wait_close(5)
        except Exception:
            pass
        print("[DB] pool closed")
    except Exception as e:
        print(f"[DB] pool close error: {type(e).__name__}: {e}")

# ---- 路由（一定放在 app 建好與 middleware 設好之後）----
from app.api.health  import router as health_router
from app.api.upload  import router as upload_router
from app.api.map     import router as map_router
from app.api.targets import router as targets_router
from app.api.auth    import router as auth_router
from app.api.stats   import router as stats_router

app.include_router(health_router,  prefix="/api/health", tags=["health"])
app.include_router(auth_router,    prefix="/api",        tags=["auth"])
app.include_router(upload_router,  prefix="/api/upload", tags=["upload"])
app.include_router(map_router,     prefix="/api",        tags=["map"])
app.include_router(targets_router, prefix="/api",        tags=["targets"])
app.include_router(stats_router,   prefix="/api",        tags=["stats"])

@app.get("/api")
def root():
    return {"app": "CellTrail", "status": "ok"}