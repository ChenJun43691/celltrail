# app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---- 先匯入 routers
from app.api.health  import router as health_router
from app.api.upload  import router as upload_router
from app.api.map     import router as map_router
from app.api.targets import router as targets_router
from app.api.auth    import router as auth_router
from app.api.stats   import router as stats_router

# ---- 建 app（一定要在 include_router 之前）
app = FastAPI(
    title="CellTrail API",
    version="0.1.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
)

# ---- CORS ----
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 掛路由（prefix 很重要）----
app.include_router(health_router,  prefix="/api/health", tags=["health"])
app.include_router(auth_router,    prefix="/api",        tags=["auth"])      # << 登入註冊在這裡
app.include_router(upload_router,  prefix="/api/upload", tags=["upload"])
app.include_router(map_router,     prefix="/api",        tags=["map"])
app.include_router(targets_router, prefix="/api",        tags=["targets"])
app.include_router(stats_router, prefix="/api",    tags=["stats"])

@app.get("/api")
def root():
    return {"app": "CellTrail", "status": "ok"}