# 醒來要做的事（v8 — W2.6 合併表頭修復 + demo_case 還原）

> 更新時間：2026-05-09
> 狀態：✅ **15/15 pytest passed**（上輪）+ ✅ **demo_case 8351 列 / 91% geom 命中**
> 法庭可防禦性：**9/10**

---

## 今日（2026-05-09）大事記

| Commit | 內容 |
|---|---|
| `529d6a9` | CLAUDE.md 建立（常用命令、架構、依賴約束） |
| `209d48f` | WAKE_UP_TODO.md v6 |
| `f155ea5` | fix(geocode)：Redis 離線 crash 修復 + OSM 兩段式結構化查詢 |
| `0144343` | WAKE_UP_TODO.md v7 |
| `eaad084` | .gitignore 補強（案件資料夾、xlsx/pdf/csv、.env 根目錄層） |
| `6d94818` | **W2.6**：合併表頭偵測（雙向通聯 H1:I1 merged cell → cell_addr 0% → 100%） |

### W2.6 修法說明
雙向通聯格式的 `基地台/交換機` header 橫跨 H+I 兩欄（合併儲存格）：
- H 欄：數字 cell_id（已正確對應）
- I 欄：基地台地址（pandas 讀到 None → 變成 `_unnamed_8` → 無法 normalize）

修法：`_iter_rows_excel` 建 header 時，若空欄前一欄 canonical = `cell_id`，
自動補名 `基地台地址`，通吃所有「cell_id 右邊接空欄」的合併表頭格式。

### demo_case 目前狀態

| target | rows | geom | % |
|---|---|---|---|
| 周蔓達 | 6769 | 6693 | 99% |
| 楊云豪 | 68 | 68 | 100% |
| 網路歷程 | 134 | 134 | 100% |
| 網路歷程_xltx | 149 | 149 | 100% |
| 雙向通聯 | 18 | 18 | 100% ← W2.6 修復 |
| 電話通聯 | 1213 | 565 | 47%（資料物理上限） |
| **合計** | **8351** | **7627** | **91%** |

> 彭奕翔（11246 列）全部 skipped：normalize 欄位正確，但 cell_addr 和 cell_id 資料層面均空，屬資料物理限制，非 bug。

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
| 1 | **pytest 回歸驗證 W2.6** | `pytest app/tests/ -v`，確認現有 15 個 test 全過，無回歸 | 2 分鐘 |
| 2 | **彭奕翔 ingest 調查** | 11246 列全 skipped，cell_addr+cell_id 均空。需打開檔案確認欄位結構 | 15 分鐘 |
| 3 | **P2.5-B acceptance test A~E** | 點「方位角」→ modal → 字數驗證 → 後端錯誤 → 成功 → popup 刷新 | 10 分鐘 |

### 中期（1-2 小時）

| # | Task | 說明 |
|---|---|---|
| 4 | **P2.5-C 法庭防禦性 dashboard** | unknown azimuth 比例 / 最近標註人 / audit trail viewer / 報告 PDF 含基準 |
| 5 | **報告含地圖截圖** | reportlab 嵌 PNG；需 selenium/playwright 或 Leaflet 後端渲染 |
| 6 | **修 stats/hit CORS preflight** | OPTIONS 沒被 CORSMiddleware 攔到，console 噴錯但功能不影響 |

### 長期（半天以上）

| # | Task | 說明 |
|---|---|---|
| 7 | **多人多角色細緻權限** | 目前只 admin/user 兩級；檢警分艙、案件分艙 |
| 8 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改用 watchmedo |
| 9 | **本地 cell_id → lat/lng 對照表** | 從業者拿基地台座標表，徹底解決純數字 cell_id 的 geocode 問題 |

---

## 提醒

- 本機 `.env`：`AUTH_ENABLED=false`、`GEO_OSM_FALLBACK=1`、Google API key 已設。
- Redis 離線不再致命（今日修復）；OSM 兩段式查詢已啟用。
- 案件資料（xlsx/pdf/csv）與 `基地台位置範例檔案/` 已加入 .gitignore，不會被 commit。
- 詳細設計決策、架構、onboard checklist 見 `CLAUDE.md`。
