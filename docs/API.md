# CellTrail API 文件

Base URL：`https://celltrail-api.onrender.com/api`（或本機 `http://localhost:8000/api`）

Swagger UI：`/api/docs`
OpenAPI JSON：`/api/openapi.json`

---

## 認證

除 `/api`、`/api/health/`、`/api/auth/login`、`/api/stats/*` 外，所有端點均需在 HTTP header 帶上：

```
Authorization: Bearer <access_token>
```

---

## 1. Health

### `GET /api/health/`

檢查 DB、PostGIS、Redis 狀態。無需登入。

**200 OK**
```json
{
  "db_ok": true,
  "db_version": "PostgreSQL 16.2 ...",
  "postgis_ok": true,
  "postgis_version": "POSTGIS=\"3.4.0\" ...",
  "redis_ok": true
}
```

## 2. Auth

### `POST /api/auth/login`

Content-Type: `application/x-www-form-urlencoded`

| 欄位 | 型別 | 必要 |
|---|---|---|
| `username` | string | ✅ |
| `password` | string | ✅ |

**200 OK**
```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

**401 Unauthorized** — 帳號或密碼錯誤。

### `GET /api/auth/me`

**200 OK**
```json
{ "id": 1, "username": "admin", "role": "admin" }
```

## 3. Users（僅 admin）

### `POST /api/users`

```json
{ "username": "alice", "password": "secret123", "role": "user" }
```

**200 OK** → `{ "id": 2, "username": "alice", "role": "user" }`
**409 Conflict** — username 已存在。

### `GET /api/users`

**200 OK**
```json
{
  "total": 2,
  "items": [
    { "id": 1, "username": "admin", "role": "admin", "created_at": "2026-04-20T..." },
    { "id": 2, "username": "alice", "role": "user",  "created_at": "2026-04-20T..." }
  ]
}
```

### `PATCH /api/users/{id}`

至少需提供一欄：
```json
{ "password": "newpass123" }
```
或
```json
{ "role": "admin" }
```

### `DELETE /api/users/{id}`

禁止刪除自己。

## 4. Upload

### `POST /api/upload`

Content-Type: `multipart/form-data`

| 欄位 | 型別 | 必要 |
|---|---|---|
| `project_id` | string | ✅ |
| `target_id` | string | 留空則以檔名為 ID |
| `file` | file | ✅ |

**支援格式**：`.csv` / `.txt` / `.tsv` / `.xlsx` / `.pdf`

**200 OK**
```json
{
  "ok": true,
  "filename": "target_a.csv",
  "project_id": "case-2026-01",
  "target_id": "target_a",
  "total": 120,
  "inserted": 115,
  "skipped": 5,
  "errors": ["row3: 缺少開始連線時間", "..."]
}
```

## 5. Map / Traces

### `GET /api/projects/{project_id}/map-layers`

Query：
- `target_id` (optional)
- `limit` (default 5000, max 10000)

**200 OK** — 標準 GeoJSON FeatureCollection
```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [121.5, 25.0] },
      "properties": {
        "target_id": "target_a",
        "start_ts": "2025-08-30T13:31:00+08:00",
        "cell_id": "1234-56",
        "cell_addr": "台北市中正區忠孝東路一段",
        "azimuth": 120,
        "accuracy_m": 150
      }
    }
  ]
}
```

### `GET /api/projects/{project_id}/unlocated`

列出 `geom IS NULL` 的資料（除錯用）。

## 6. Targets

### `DELETE /api/projects/{project_id}/targets/{target_id}`

刪除某個目標在指定案件下的所有軌跡。

**200 OK** → `{ "ok": true, "deleted": 123, "project_id": "...", "target_id": "..." }`
**404 Not Found** — 該 target 不存在或已刪除。

## 7. Geocode

### `GET /api/geocode?address=...&use_cache=true`

手動地理編碼，主要供除錯或前端預查。

**200 OK**
```json
{
  "query": "台北市中正區忠孝東路一段1號",
  "formatted_address": "100 台灣台北市中正區忠孝東路一段1號",
  "lat": 25.0452,
  "lng": 121.5239,
  "place_id": "ChIJ...",
  "types": ["street_address"],
  "partial_match": false,
  "cache": "hit"
}
```

## 8. Stats

### `POST /api/stats/hit`

前端載入時打一次，計數使用量（同 IP 一小時內去重）。

### `GET /api/stats`

```json
{ "ok": true, "total": 1234, "today": 56, "date": "20260420" }
```

## 錯誤回應格式

FastAPI 預設：
```json
{ "detail": "錯誤訊息" }
```
