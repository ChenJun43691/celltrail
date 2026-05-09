# 醒來要做的事（v9 — 三個 ingest bug 修復 + demo_case 98%）

> 更新時間：2026-05-09
> 狀態：✅ **92/92 pytest passed** + ✅ **demo_case 30843 列 / 98% geom**
> 法庭可防禦性：**9/10**

---

## 今日（2026-05-09）大事記

| Commit | 內容 |
|---|---|
| `529d6a9` | CLAUDE.md 建立 |
| `f155ea5` | geocode: Redis crash 修復 + OSM 兩段式查詢 |
| `eaad084` | .gitignore 補強（案件資料/secrets） |
| `6d94818` | W2.6: 合併表頭偵測（雙向通聯 H1:I1 → cell_addr 0%→100%） |
| `23d87b4` | **三個 ingest bug 修復**（見下） |

### `23d87b4` 三個連環 bug 說明

| # | Bug | 根因 | 修法 | 結果 |
|---|---|---|---|---|
| 1 | carrier_profile DB 缺 14 個 W2.x key | `get_active_header_map()` DB 成功就不 fallback → `迄基地台` 等新欄不存在 | `get_active_header_map()` 改為 DB + `_RAW2CANON` 合併（DB 優先） | 彭奕翔 cell_id 從 None → 正確值 |
| 2 | 合併表頭空欄被 `_unnamed_N` 丟棄 | 雙向通聯 H1:I1 merged cell，I欄=地址，pandas 讀到 None | `_iter_rows_excel` 偵測 cell_id 前欄 → 補名 `基地台地址` | 雙向通聯 geom 0% → 100% |
| 3 | ingest 無請求內快取 → API 超時 | 11246 列 × 119 唯一地址，每列都打 Google API → ~47 分鐘 | `_ingest_rows_stream` 加 `_geo_cache` dict，同 upload 同地址只查一次 | 彭奕翔上傳 270s 超時 → 31s |

### demo_case 最終狀態

| target | rows | geom % |
|---|---|---|
| 彭奕翔 | 22492 | **100%** |
| 周蔓達 | 6769 | 99% |
| 楊云豪 | 68 | 100% |
| 網路歷程 | 134 | 100% |
| 網路歷程_xltx | 149 | 100% |
| 雙向通聯 | 18 | **100%** |
| 電話通聯 | 1213 | 47%（資料物理上限） |
| **合計** | **30843** | **98%** |

---

## 快速啟動（下次 session 標準程序）

```bash
open -a Docker
docker compose -f infra/docker-compose.yml up -d db
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
source .venv/bin/activate
uvicorn app.main:app --port 8000 &
cd ../frontend && python3 -m http.server 5501
# http://127.0.0.1:5501  admin/admin123
```

---

## 待辦（依優先級）

### 立即可做（≤30 分鐘）

| # | Task | 說明 | 估計 |
|---|---|---|---|
| 1 | **pytest 回歸（含 carrier_profile 合併新邏輯）** | `pytest app/tests/ -v`，特別確認 carrier_profile + ingest 相關 test | 2 分鐘 |
| 2 | **P2.5-B acceptance test A~E** | 點「方位角」→ modal → 字數驗證 → 後端錯誤 → 成功 → popup 刷新 | 10 分鐘 |
| 3 | **彭奕翔 ingest 去重** | 目前 22492 列（=11246×2，因第一次超時後資料未入 DB，第二次成功）確認是否有重複 | 5 分鐘 |

### 中期（1-2 小時）

| # | Task | 說明 |
|---|---|---|
| 4 | **P2.5-C 法庭防禦性 dashboard** | unknown azimuth 比例 / 最近標註人 / audit trail viewer / 報告 PDF 含基準 |
| 5 | **報告含地圖截圖** | reportlab 嵌 PNG；需 selenium/playwright 或 Leaflet 後端渲染 |
| 6 | **修 stats/hit CORS preflight** | OPTIONS 沒被 CORSMiddleware 攔到，console 噴錯但功能不影響 |
| 7 | **carrier_profile DB 同步** | 把 _RAW2CANON 所有 key 補進 DB mapping_json（讓 DB 真正成為 SoT） |

### 長期（半天以上）

| # | Task | 說明 |
|---|---|---|
| 8 | **多人多角色細緻權限** | 目前只 admin/user 兩級 |
| 9 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改 watchmedo |
| 10 | **本地 cell_id → lat/lng 對照表** | 從業者拿基地台座標表，徹底解決純數字 cell_id 的 geocode 問題 |

---

## 提醒

- 本機 `.env`：`AUTH_ENABLED=false`、`GEO_OSM_FALLBACK=1`、Google API key 已設。
- Redis 離線不再致命（已修）；OSM 兩段式查詢已啟用。
- carrier_profile 現在合併 DB + _RAW2CANON，新增 `_RAW2CANON` 欄位自動生效，無需 DB migration。
- 案件資料已加入 .gitignore，不會被 commit。
- 詳細架構與 onboard checklist 見 `CLAUDE.md`。
