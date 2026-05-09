# 醒來要做的事（v7 — OSM 備援啟用 + geocode bug 修復）

> 更新時間：2026-05-09
> 狀態：✅ **15/15 pytest passed**（上輪驗證）+ ✅ **OSM 備援全鏈路驗證通過**
> 法庭可防禦性：**9/10**

---

## 今日（2026-05-09）大事記

1. **CLAUDE.md 建立**（`529d6a9`）— 常用命令、架構、依賴約束、onboard checklist
2. **WAKE_UP_TODO.md v6**（`209d48f`）— 更新進度與待辦
3. **geocode 兩個 bug 修復 + OSM 兩段式查詢**（`f155ea5`）：
   - Bug 1：`_cache_get` 的 `_r.get()` 在 try/except 外，Redis 離線時 `ConnectionRefused` 向上拋出，導致 Google geocode 也失敗（geom 全 NULL）→ 移入 try/except 修復
   - Bug 2：`_osm_geocode` 自由格式對台灣中文地址命中率 0% → 新增 Pass 2 結構化查詢（號碼前置格式 `city=高雄市 street=211號中正四路`）
   - `.env` 加入 `GEO_OSM_FALLBACK=1` + `NOMINATIM_EMAIL=chen95572295@gmail.com`
4. **驗證結果**：`網路歷程.xlsx` 134/134 geom 命中率 **100%**；`/map-layers` 回傳 134 features ✅

---

## 快速啟動（下次 session 標準程序）

```bash
# 1) Docker 確認
open -a Docker   # 若 daemon 沒跑
docker compose -f infra/docker-compose.yml up -d db

# 2) venv 體檢
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
source .venv/bin/activate
pip check

# 3) 啟 uvicorn（Terminal A）
uvicorn app.main:app --port 8000

# 4) 啟前端（Terminal B）
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/frontend
python3 -m http.server 5501
# http://127.0.0.1:5501
```

---

## 待辦（依優先級）

### 立即可做（≤30 分鐘）

| # | Task | 說明 | 估計 |
|---|---|---|---|
| 1 | **清 demo_case / osm_test 舊資料** | 測試上傳留下 geom NULL 記錄（osm_test_01/02）+ 重複 evidence_files；影響地圖乾淨度 | 2 分鐘 |
| 2 | **重新 ingest 楊云豪 PDF** | 68 列 + OSM 備援啟用後重跑，約 70 秒；跑完 `/map-layers` 應有點位 | 2 分鐘 |
| 3 | **P2.5-B acceptance test A~E** | 點「方位角」→ modal → 字數驗證 → 後端錯誤 → 成功 → popup 刷新 | 10 分鐘 |

### 中期（1-2 小時）

| # | Task | 說明 |
|---|---|---|
| 4 | **P2.5-C 法庭防禦性 dashboard** | unknown 比例 / 最近標註人 / audit_logs trail viewer / 報告 PDF 含北方基準 |
| 5 | **報告含地圖截圖** | reportlab 嵌 PNG；需 selenium/playwright 或 Leaflet 後端渲染 |
| 6 | **修 stats/hit CORS preflight** | OPTIONS preflight 沒被 CORSMiddleware 攔到，console 一直噴錯，功能不影響 |

### 長期（半天以上）

| # | Task | 說明 |
|---|---|---|
| 7 | **多人多角色細緻權限** | 目前只 admin/user 兩級；檢警分艙、案件分艙 |
| 8 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改用 watchmedo 繞過 |
| 9 | **本地 cell_id → lat/lng 對照表** | 從業者拿基地台座標表，徹底解決 geocode 依賴 |

---

## 提醒

- 本機 `.env` 仍是 `AUTH_ENABLED=false`（anonymous admin）。Production 務必改 `true`。
- OSM 備援已啟用（`GEO_OSM_FALLBACK=1`）；Redis 離線不再影響 geocode（已修）。
- OSM 兩段式查詢：Pass 1 自由格式 → Pass 2 結構化（號碼前置）；台灣地址覆蓋率大幅提升但非 100%（OSM 資料庫本身不完整）。
- 詳細設計決策、環境陷阱、onboard checklist 見 `CLAUDE.md`。
