# 醒來要做的事（v10 — P4.x 系列完成）

> 更新時間：2026-05-10
> 狀態：✅ 92/92 pytest passed ｜ ✅ demo_case 30843 列 / 98% geom ｜ 法庭可防禦性 9/10

---

## 本輪（2026-05-10）大事記

| Commit | 內容 |
|---|---|
| `adb4f7c` | P4.2: 地圖「顯示定位時間」標籤功能 |
| `eef1f11` | P4.3: 臨時使用 vs 專案管理模式切換系統 |
| `9dbf288` | P4.3-finish: 案件名稱下拉自動補全 + 模式切換串接 |
| `67b07f5` | P4.4-v2: login.html 全面改版—科技感深藍系設計 |
| `dac4a9b` | refactor: Logo 移入卡片頂端，重組版面結構 |
| 本輪 | register.html / change-password.html 統一視覺風格 |

### P4.x 功能摘要

| 功能 | 說明 |
|---|---|
| **P4.1 本地基地台座標表** | cell_towers DB + admin import API + admin UI |
| **P4.2 顯示定位時間** | 地圖時間標籤，zoom 響應字體，per-series 切換 |
| **P4.3 臨時 vs 專案模式** | parse-temp API、session 狀態、模式選擇 modal、案件下拉清單 |
| **P4.4 登入頁改版** | 影片背景、毛玻璃卡片、科技感深藍系、register/change-password 同步 |

---

## 快速啟動

```bash
open -a Docker
docker compose -f infra/docker-compose.yml up -d db
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
source .venv/bin/activate
uvicorn app.main:app --port 8000 &
cd ../frontend && python3 -m http.server 5501
# 登入頁：http://127.0.0.1:5501/login.html
# 主系統：http://127.0.0.1:5501/index.html（AUTH_ENABLED=false 自動匿名 admin）
```

---

## 待辦（依優先級）

### 中期功能（1-2 小時）

| # | Task | 說明 |
|---|---|---|
| 1 | **P2.5-C 法庭防禦性 dashboard** | unknown azimuth 比例 / 最近標註人 / audit trail viewer / 報告 PDF 含基準 |
| 2 | **報告含地圖截圖** | reportlab 嵌 PNG；需 selenium/playwright 或 Leaflet 後端渲染 |
| 3 | **修 stats/hit CORS preflight** | OPTIONS 沒被 CORSMiddleware 攔到，console 噴錯但功能不影響 |
| 4 | **carrier_profile DB 同步** | 把 _RAW2CANON 所有 key 補進 DB mapping_json（讓 DB 真正成為 SoT） |

### 長期（半天以上）

| # | Task | 說明 |
|---|---|---|
| 5 | **多人多角色細緻權限** | 目前只 admin/user 兩級 |
| 6 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改 watchmedo |
| 7 | **本地 cell_id 座標表填充** | 從業者拿基地台座標 CSV，徹底解決純數字 cell_id 的 geocode 問題 |

---

## 提醒

- 本機 `.env`：`AUTH_ENABLED=false`、`GEO_OSM_FALLBACK=1`、Google API key 已設。
- Redis 離線不再致命（已修）；OSM 兩段式查詢已啟用。
- carrier_profile 現在合併 DB + _RAW2CANON，新增欄自動生效，無需 DB migration。
- 案件資料已加入 .gitignore，不會被 commit。
- 詳細架構與 onboard checklist 見 `CLAUDE.md`。
