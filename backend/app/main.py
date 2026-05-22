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

import logging
import os
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from app.db.session import pool, get_conn
from app.services.limiter import limiter
from app.security import SECRET_KEY, AUTH_ENABLED

logger = logging.getLogger("celltrail")


# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # === startup ===
    print("[CORS] allow_origins =", allow_origins)
    _config_safety_audit()
    try:
        pool.open()
        pool.wait(10)
        print(f"[DB] pool ready (min={pool.min_size}, max={pool.max_size})")
    except Exception as e:
        print(f"[DB] pool warmup error: {type(e).__name__}: {e}")

    # APScheduler：每 6 小時 ping 一次 DB，保 Supabase 免費方案不被暫停。
    # 整段包 try-catch；pytest 下不啟動（測試不需保活，避免背景執行緒干擾）。
    if "pytest" not in sys.modules:
        try:
            scheduler = BackgroundScheduler(daemon=True)
            scheduler.add_job(
                _supabase_keepalive,
                trigger="interval",
                hours=6,
                id="supabase_keepalive",
                next_run_time=datetime.now(timezone.utc),  # 啟動後立即跑一次，之後每 6h
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            app.state.keepalive_scheduler = scheduler
            print("[keepalive] scheduler started（每 6 小時 ping 一次 DB）")
        except Exception as e:
            app.state.keepalive_scheduler = None
            print(f"[keepalive] scheduler 啟動失敗（不影響主程式）: {type(e).__name__}: {e}")
    else:
        app.state.keepalive_scheduler = None

    yield

    # === shutdown ===
    try:
        sched = getattr(app.state, "keepalive_scheduler", None)
        if sched is not None:
            sched.shutdown(wait=False)
            print("[keepalive] scheduler stopped")
    except Exception as e:
        print(f"[keepalive] scheduler shutdown error: {type(e).__name__}: {e}")

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


# ---------- 啟動設定安全自檢 ----------
def _config_safety_audit() -> None:
    """啟動時檢查不適合正式環境的設定，misconfiguration 即大聲警告。

    只印警告、不改變任何行為。對應 docs/部署檢查清單.md A 區 ——
    把「靠人記得檢查的清單」變成「系統啟動時自己檢查」。
    """
    warnings: list[str] = []

    # SECRET_KEY：JWT 簽章金鑰。預設值或過短 → 可被偽造 token、冒充 admin。
    if SECRET_KEY in ("change-me-please", ""):
        warnings.append("SECRET_KEY 仍是預設值 —— 正式環境務必用 `openssl rand -hex 32` 重產")
    elif len(SECRET_KEY) < 32:
        warnings.append(
            f"SECRET_KEY 僅 {len(SECRET_KEY)} 字元、過短 —— 正式環境請用 "
            "`openssl rand -hex 32`（64 字元）重產"
        )

    # AUTH_ENABLED=false：所有請求都以 anonymous admin 通行。
    if not AUTH_ENABLED:
        warnings.append(
            "AUTH_ENABLED=false —— 所有請求以 anonymous admin 通行，正式環境務必設 true"
        )

    # CORS：AUTH 已開（疑似正式環境）卻仍允許本機來源 → 多半忘了設正式網域。
    if AUTH_ENABLED:
        local = [o for o in allow_origins if "localhost" in o or "127.0.0.1" in o]
        if local:
            warnings.append(
                f"CORS 白名單仍含本機來源 {local} —— 正式環境請以環境變數 "
                "CORS_ORIGINS 設為正式前端網域"
            )

    if warnings:
        print("=" * 70)
        print("[CONFIG WARNING] 偵測到不適合正式環境的設定（僅警告，不影響啟動）：")
        for w in warnings:
            print(f"  ⚠  {w}")
        print("  完整檢查項目見 docs/部署檢查清單.md")
        print("=" * 70)
    else:
        print("[CONFIG] 設定安全自檢通過")


# ---------- Supabase 保活（APScheduler）----------
def _supabase_keepalive() -> None:
    """對 DB 跑一次 SELECT 1，避免 Supabase 免費方案因一週無活動而自動暫停。

    由 APScheduler 每 6 小時呼叫一次。全程包 try-catch：保活失敗只寫 log，
    絕不影響主程式。
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1", prepare=False)
            cur.fetchone()
        print("[keepalive] Supabase ping OK")
    except Exception as e:
        print(f"[keepalive] ping failed: {type(e).__name__}: {e}")


# ---------- FastAPI App ----------
app = FastAPI(
    title="CellTrail API",
    version="0.2.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# slowapi：Rate limiter 掛在 app.state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 全局 500 錯誤處理：不洩漏 stack trace ----------
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception %s %s — %s\n%s",
        request.method, request.url.path,
        type(exc).__name__,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "系統發生錯誤，請稍後再試"},
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
from app.api.members     import router as members_router      # noqa: E402
from app.api.requests    import router as requests_router     # noqa: E402
from app.api.cell_towers     import router as cell_towers_router      # noqa: E402
from app.api.carrier_profile import router as carrier_profile_router  # noqa: E402
from app.api.parse_only      import router as parse_only_router       # noqa: E402
from app.api.format_reports  import router as format_reports_router   # noqa: E402
from app.api.share           import router as share_router            # noqa: E402

app.include_router(health_router,          prefix="/api/health", tags=["health"])
app.include_router(auth_router,            prefix="/api",        tags=["auth"])
app.include_router(users_router,           prefix="/api",        tags=["users"])
app.include_router(upload_router,          prefix="/api/upload", tags=["upload"])
app.include_router(map_router,             prefix="/api",        tags=["map"])
app.include_router(targets_router,         prefix="/api",        tags=["targets"])
app.include_router(stats_router,           prefix="/api",        tags=["stats"])
app.include_router(geocode_router,         prefix="/api",        tags=["geocode"])
app.include_router(audit_router,           prefix="/api",        tags=["audit"])
app.include_router(report_router,          prefix="/api",        tags=["report"])
app.include_router(members_router,         prefix="/api",        tags=["members"])
app.include_router(requests_router,        prefix="/api",        tags=["account-requests"])
app.include_router(cell_towers_router,     prefix="/api",        tags=["cell-towers"])
app.include_router(carrier_profile_router, prefix="/api",        tags=["carrier-profile"])
app.include_router(parse_only_router,      prefix="/api",        tags=["parse-only"])
app.include_router(format_reports_router,  prefix="/api",        tags=["format-reports"])
app.include_router(share_router,           prefix="/api",        tags=["share"])


@app.get("/api")
def root():
    return {"app": "CellTrail", "version": "0.2.0", "status": "ok"}
