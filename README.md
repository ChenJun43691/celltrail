# CellTrail

**基地台訊號軌跡視覺化系統** — 將通聯紀錄（CSV / TXT / XLSX / PDF）自動清洗、地理編碼後，以 Leaflet 地圖呈現時序軌跡與圖層。

![status](https://img.shields.io/badge/status-beta-yellow)
![python](https://img.shields.io/badge/python-3.13-blue)
![fastapi](https://img.shields.io/badge/FastAPI-0.115-009688)
![postgis](https://img.shields.io/badge/PostgreSQL-16%2BPostGIS-336791)

## 特色

- **多格式匯入**：CSV、TXT、TSV、XLSX、PDF 一鍵上傳，後端自動解析（手機拍攝的 PDF 報表亦可）
- **欄位自動對照**：中文表頭（「開始連線時間」「基地台地址」「方位角」…）自動標準化，支援繁簡與全形半形
- **兩階段地理編碼**：Google Maps 為主，OpenStreetMap Nominatim 為備援，並以 Redis 做 30 天快取
- **時空查詢**：以 `project_id` / `target_id` 為單位，回傳 GeoJSON FeatureCollection
- **權限管理**：JWT + bcrypt/pbkdf2，admin 可建立與管理使用者
- **輕量前端**：單檔 HTML + Leaflet，無需建置工具

## 系統架構

```
┌──────────┐   HTTPS   ┌──────────────┐   pool   ┌──────────────────┐
│ Frontend │ ────────▶ │  FastAPI     │ ───────▶ │ PostgreSQL       │
│ (Leaflet)│           │  Uvicorn     │          │  + PostGIS       │
└──────────┘           └──────┬───────┘          └──────────────────┘
                              │
                              ├────────▶ Redis（地理快取 / 統計）
                              │
                              └────────▶ Google Geocoding API
                                        └──▶ OSM Nominatim（備援）
```

## 目錄結構

```
CellTrail/
├── backend/                      # FastAPI 後端
│   ├── app/
│   │   ├── main.py              # 入口（lifespan、CORS、router）
│   │   ├── security.py          # JWT、密碼、權限
│   │   ├── api/                 # REST 端點（health/auth/users/upload/map/...）
│   │   ├── services/            # 領域服務（ingest / geocode）
│   │   ├── db/
│   │   │   ├── session.py       # 連線池（psycopg3）
│   │   │   └── schema.sql       # DDL
│   │   └── tests/               # pytest smoke test
│   ├── requirements.txt
│   └── .env.example
├── frontend/                     # 靜態前端
│   ├── index.html               # 主頁（Leaflet）
│   └── api.js                   # 登入/登出
├── infra/
│   └── docker-compose.yml       # PostGIS + Redis + mbtileserver
├── scripts/
│   └── bootstrap.sh             # 環境檢核
├── docs/
│   ├── SPEC.md
│   └── API.md
└── data/
    └── tiles/                   # 離線地圖圖磚（可選）
```

## 快速啟動（本機開發）

### 1. 前置需求

macOS / Linux、Docker、Python 3.13、Node（可選，僅前端若要用 live-server 需要）。

### 2. 啟動基礎設施

```bash
cd infra
docker compose up -d        # 啟動 PostGIS / Redis / tileserver
cd ..
./scripts/bootstrap.sh      # 檢查依賴並等待 DB 就緒
```

### 3. 建立資料庫 Schema

```bash
docker exec -i celltrail_db \
    psql -U celltrail -d celltrail < backend/app/db/schema.sql
```

> 首次執行會建立一個預設 admin 帳號：**admin / admin123**。
> **請務必在上線前透過 `PATCH /api/users/{id}` 修改密碼**。

### 4. 啟動後端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env         # 編輯 SECRET_KEY、GOOGLE_MAPS_API_KEY 等
uvicorn app.main:app --reload --port 8000
```

開啟 <http://localhost:8000/api/docs> 即可看到 Swagger UI。

### 5. 啟動前端

```bash
cd frontend
# 任一靜態伺服器皆可，例如：
python3 -m http.server 5500
# 或：
# npx serve -p 5500
```

開啟 <http://localhost:5500>，用預設 `admin / admin123` 登入。

## 環境變數

完整清單見 [`backend/.env.example`](backend/.env.example)。關鍵項目：

| 變數 | 必要 | 說明 |
|---|---|---|
| `DATABASE_URL` | ✅ | Postgres DSN（支援 `postgres://` 自動轉換） |
| `SECRET_KEY` | ✅ | JWT 簽章金鑰，生產環境務必為強隨機 |
| `CORS_ORIGINS` | ✅ | 允許的前端來源（逗號分隔） |
| `REDIS_URL` | ✅ | Redis 連線字串 |
| `GOOGLE_MAPS_API_KEY` | ⚠️ | 無此 key 則 `/api/geocode` 回 500 |
| `GEO_OSM_FALLBACK` | ❌ | 設為 `1` 啟用 OSM 備援 |
| `DB_POOL_MAX` | ❌ | 連線池上限，預設 5 |

## 測試

```bash
cd backend
source .venv/bin/activate
pip install pytest httpx
pytest app/tests/ -v
```

smoke test 不依賴外部服務（DB / Redis / Google），可在 CI 直接執行。

## 部署

**後端**：Render（已設定 `gunicorn` + `uvicorn` worker）
**前端**：Netlify（靜態部署 `frontend/` 即可）
**資料庫**：Supabase 或自建 PostgreSQL 16+（需 PostGIS 與 pgcrypto 擴充）

生產環境檢查清單：
- `SECRET_KEY` 已改為強隨機
- `CORS_ORIGINS` 只列實際前端網域
- `admin` 預設密碼已變更
- PostgreSQL 已啟用 `postgis` 與 `pgcrypto`

## 授權

見 [`LICENSE`](LICENSE)。
