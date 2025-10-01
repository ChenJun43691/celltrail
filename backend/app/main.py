# app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---- 建 app（一定要在 include_router 之前）
app = FastAPI(
    title="CellTrail API",
    version="0.1.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
)

# ---- CORS（單一來源：以環境變數為主）----
# 在 .env（本機）或 Render 環境變數設：
#   CORS_ORIGINS = https://celltrail.netlify.app,http://localhost:5500,http://127.0.0.1:5500,http://localhost:5173
raw = os.getenv(
    "CORS_ORIGINS",
    "https://celltrail.netlify.app"  # 預設只開 Netlify 站
)

allow_origins = []
for o in (x.strip() for x in raw.split(",") if x.strip()):
    o = o.rstrip("/")  # 去尾斜線
    o = o.replace("HTTP://", "http://").replace("HTTPS://", "https://")
    allow_origins.append(o)

# （可選）一次放行所有 *.netlify.app（風險較大；需要時在 Render 設 CORS_NETLIFY_REGEX=1）
allow_origin_regex = r"^https://.*\.netlify\.app$" if os.getenv("CORS_NETLIFY_REGEX") == "1" else None

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,           # 例如 ["https://celltrail.netlify.app", "http://localhost:5500", ...]
    allow_origin_regex=allow_origin_regex, # 二選一：要開 regex 就在環境變數開啟
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],                   # 允許 Authorization / Content-Type 等
)

# 啟動時印出以便在 Render log 檢查
@app.on_event("startup")
async def show_cors():
    print("[CORS] allow_origins      =", allow_origins)
    print("[CORS] allow_origin_regex =", allow_origin_regex)

# ---- 匯入 routers（放在 app/middleware 設好之後）----
from app.api.health  import router as health_router
from app.api.upload  import router as upload_router
from app.api.map     import router as map_router
from app.api.targets import router as targets_router
from app.api.auth    import router as auth_router
from app.api.stats   import router as stats_router

# ---- 掛路由（prefix 很重要）----
app.include_router(health_router,  prefix="/api/health", tags=["health"])
app.include_router(auth_router,    prefix="/api",        tags=["auth"])
app.include_router(upload_router,  prefix="/api/upload", tags=["upload"])
app.include_router(map_router,     prefix="/api",        tags=["map"])
app.include_router(targets_router, prefix="/api",        tags=["targets"])
app.include_router(stats_router,   prefix="/api",        tags=["stats"])

@app.get("/api")
def root():
    return {"app": "CellTrail", "status": "ok"}