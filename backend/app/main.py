# backend/app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.db.session import pool

# ---- 建 app（先建 app，再註冊事件）----
app = FastAPI(
    title="CellTrail API",
    version="0.1.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
)

# ---- CORS（以環境變數為主；預設只開 Netlify 正站）----
raw = os.getenv("CORS_ORIGINS", "https://celltrail.netlify.app")
allow_origins = []
for o in (x.strip() for x in raw.split(",") if x.strip()):
    o = o.rstrip("/")
    o = o.replace("HTTP://", "http://").replace("HTTPS://", "https://")
    allow_origins.append(o)

# 可選：放行 *.netlify.app
allow_origin_regex = r"^https://.*\.netlify\.app$" if os.getenv("CORS_NETLIFY_REGEX") == "1" else None

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_origin_regex=allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 事件 ----
@app.on_event("startup")
async def startup():
    print("[CORS] allow_origins      =", allow_origins)
    print("[CORS] allow_origin_regex =", allow_origin_regex)
    try:
        pool.open()
        pool.wait(10)
        print(f"[DB] pool ready (min={pool.min_size}, max={pool.max_size})")
    except Exception as e:
        print(f"[DB] pool warmup error: {type(e).__name__}: {e}")

@app.on_event("shutdown")
async def shutdown():
    try:
        pool.close()
        try:
            # psycopg_pool >= 3.2
            pool.wait_close(10)
        except Exception:
            pass
        print("[DB] pool closed")
    except Exception as e:
        print(f"[DB] pool close error: {type(e).__name__}: {e}")

# ---- 路由 ----
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