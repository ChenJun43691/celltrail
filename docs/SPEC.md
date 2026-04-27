# CellTrail 系統規格書

版本：v0.2.0
最後更新：2026-04-20

---

## 1. 系統目標

將電信通聯紀錄（基地台連線明細）自動化地：

1. 解析多種來源格式（CSV / TXT / XLSX / PDF）
2. 清洗欄位並標準化時間、地址、方位角
3. 以地址或 `cell_id` 進行地理編碼
4. 存入 PostGIS，以 GeoJSON 對外提供
5. 在 Leaflet 地圖上視覺化時序軌跡

主要使用情境：刑事偵查輔助、電信網路分析、行動軌跡回溯。

## 2. 系統元件

| 元件 | 技術 | 職責 |
|---|---|---|
| Frontend | 單檔 HTML + Leaflet + nouislider | 地圖繪製、上傳、時間篩選 |
| API Gateway | FastAPI (Uvicorn/Gunicorn) | REST 端點、JWT 驗證 |
| Ingest Service | Python (pandas / pdfplumber) | 多格式解析與欄位對照 |
| Geocode Service | requests + Redis | Google Maps 為主、OSM 為備援 |
| Database | PostgreSQL 16 + PostGIS | 軌跡資料儲存與空間查詢 |
| Cache | Redis 7 | 地理編碼快取、使用統計計數 |

## 3. 資料模型

### 3.1 `users`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `username` | TEXT UNIQUE | 登入帳號 |
| `password_hash` | TEXT | bcrypt / pbkdf2 / pgcrypto 相容 |
| `role` | TEXT | `admin` 或 `user` |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | |

### 3.2 `raw_traces`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `project_id` | TEXT | 案件編號（必填） |
| `target_id` | TEXT | 目標識別碼（必填） |
| `start_ts` | TIMESTAMPTZ | 開始連線時間 |
| `end_ts` | TIMESTAMPTZ | 結束連線時間 |
| `cell_id` | TEXT | 基地台編號 |
| `cell_addr` | TEXT | 基地台地址 |
| `sector_name` | TEXT | 細胞/小區名稱 |
| `site_code` | TEXT | 台號/站號 |
| `sector_id` | TEXT | 細胞編號 |
| `azimuth` | INT | 方位角（0-359） |
| `lat` / `lng` | DOUBLE PRECISION | 原始座標（除錯用） |
| `accuracy_m` | INT | 預估誤差半徑（公尺） |
| `geom` | geometry(Point,4326) | PostGIS 幾何欄位 |
| `created_at` | TIMESTAMPTZ | |

**索引策略**：
- `(project_id, target_id)` — 查詢地圖圖層的主要條件
- `start_ts` — 時間篩選
- GIST `geom` — 空間查詢
- `cell_id` — 以基地台反查

## 4. 資料流

```
 使用者
   │
   │ (1) 上傳 CSV/XLSX/PDF
   ▼
 POST /api/upload   ───── JWT 驗證
   │
   ▼
 ingest_auto(filename, bytes)
   │
   ├─ ingest_pdf        → pdfplumber 解析表格
   ├─ _iter_rows_excel  → pandas + openpyxl
   └─ _iter_rows_csv    → csv.DictReader
   │
   ▼
 _normalize_row()  ← HEADER_MAP 欄位對照
   │
   ▼
 geocode.lookup(cell_id, cell_addr)
   ├─ 本地字典（預留）
   ├─ Redis cache
   ├─ Google Geocoding API
   └─ OSM Nominatim（若 GEO_OSM_FALLBACK=1）
   │
   ▼
 INSERT INTO raw_traces (...) — 自動產生 geom
   │
   ▼
 回傳 {total, inserted, skipped, errors}
```

## 5. 權限設計

| 角色 | 權限 |
|---|---|
| `user` | 上傳資料、查詢自己所屬 project 的地圖 |
| `admin` | 使用者管理、所有 user 權限 |

**未來擴充**：目前 `project_id` 與使用者並無關聯表，任何登入者都可存取所有 project。若需嚴格隔離，應補上 `user_projects` 多對多關聯表。

## 6. 安全性

- **密碼**：bcrypt（主）/ pbkdf2_sha256 / pgcrypto `crypt()`（舊資料相容）
- **Token**：JWT (HS256)，預設 8 小時過期
- **CORS**：白名單模式，避免 regex 誤判
- **SQL injection**：全部用 parameterized query
- **Server-side prepared**：已關閉（`prepare_threshold=0`），避免連接池與某些 pooler（如 pgbouncer）相容性問題

## 7. 未來規劃

- 多目標軌跡路徑模擬（已在前端實作，後端尚無 API 輔助）
- 基地台字典表（`cell_sites`）取代每次 geocode
- 匯入任務非同步化（Celery / RQ）
- 軌跡熱區分析（PostGIS `ST_ClusterDBSCAN`）
