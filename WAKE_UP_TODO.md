# 醒來要做的事（v6 — CLAUDE.md 建立完成）

> 更新時間：2026-05-09
> 狀態：✅ **15/15 pytest passed**（上輪驗證）+ ✅ **CLAUDE.md 已 commit + push**
> 法庭可防禦性：**9/10**（P0+P1+P2+P2.5-A+P2.5-B+P2.6 全完成）

---

## 今日（2026-05-09）大事記

1. 建立 / 更新 `CLAUDE.md`（`529d6a9`）
   - 新增「常用命令」章節：基礎設施啟動、後端/前端 dev server、pytest 單一測試指令、smoke_audit.sh、venv 救援
   - 新增「關鍵依賴版本約束」：bcrypt==4.2.1 pin、psycopg prepare=False、pytest==8.3.3
   - 保留所有原有內容（設計決策、待辦、環境陷阱、commit 風格、onboard checklist）
2. Push 到 `origin/main`（`b62a64d` → `529d6a9`）

---

## 快速啟動（下次 session 標準程序）

```bash
# 1) Docker 確認
open -a Docker   # 若 daemon 沒跑
docker compose -f infra/docker-compose.yml up -d db

# 2) venv 體檢
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
source .venv/bin/activate
pip check   # 預期：No broken requirements found
# 若有問題：bash scripts/rebuild_venv.sh

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
| 1 | **啟用 OSM 備援** | `.env` 加 `GEO_OSM_FALLBACK=1` + `NOMINATIM_EMAIL=你的email`；geocode 全敗時地圖空白的根本解 | 5 分鐘 |
| 2 | **清 demo_case 舊資料** | 4 次失敗上傳留下 `geom IS NULL` 記錄 + 對應 evidence_files；影響 P2.5-B acceptance test | 1 分鐘 |
| 3 | **重新 ingest 楊云豪 PDF** | 啟 OSM 後重跑；68 列 ~70 秒；跑完 `/map-layers` 應有點位 | 2 分鐘 |
| 4 | **P2.5-B acceptance test A~E** | 點「方位角」→ modal → 字數驗證 → 後端錯誤 → 成功 → popup 刷新 | 10 分鐘 |

### 中期（1-2 小時）

| # | Task | 說明 |
|---|---|---|
| 5 | **P2.5-C 法庭防禦性 dashboard** | unknown 比例 / 最近標註人 / audit_logs trail viewer / 報告 PDF 含北方基準 |
| 6 | **報告含地圖截圖** | reportlab 嵌 PNG；需 selenium/playwright 或 Leaflet 後端渲染 |
| 7 | **修 stats/hit CORS preflight** | OPTIONS preflight 沒被 CORSMiddleware 攔到，console 一直噴錯，功能不影響 |

### 長期（半天以上）

| # | Task | 說明 |
|---|---|---|
| 8 | **多人多角色細緻權限** | 目前只 admin/user 兩級；檢警分艙、案件分艙 |
| 9 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改用 watchmedo 繞過 |
| 10 | **本地 cell_id → lat/lng 對照表** | 從業者拿基地台座標表，徹底解決 geocode 依賴 |

---

## 提醒

- 本機 `.env` 仍是 `AUTH_ENABLED=false`（anonymous admin）。Production 務必改 `true`。
- Redis 沒跑會噴 `ConnectionRefused`，**不致命**，但 geocode cache 和 stats dedup 會失效。
- Schema 套用腳本是 idempotent（`IF NOT EXISTS`），重跑安全，但要等 Docker 容器 `(healthy)` 再跑。
- 詳細設計決策、環境陷阱、onboard checklist 見 `CLAUDE.md`。
