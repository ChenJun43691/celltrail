from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

from app.api.health import router as health_router
from app.api.upload import router as upload_router
from app.api.map import router as map_router  # 只引用，不在 main 再宣告
from app.api.geocode import router as geocode_router

app = FastAPI(title="CellTrail API", version="0.1.0")

# CORS
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 路由註冊
app.include_router(health_router, prefix="/api/health", tags=["health"])
app.include_router(upload_router, prefix="/api/upload", tags=["upload"])
app.include_router(map_router, prefix="/api", tags=["map"])
app.include_router(geocode_router, prefix="/api", tags=["geocode"])

@app.get("/api")
def root():
    return {"app": "CellTrail", "status": "ok"}