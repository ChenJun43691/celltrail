# 醒來要做的事（v14 — addr 真因 + 業務邏輯測試補強）

> 更新時間：2026-05-24
> 狀態：✅ 164/164 pytest passed ｜ 證據完整性 9.5/10（上傳/標記數字不再沉默）

---

## 本輪（2026-05-24）大事記

| Commit | 內容 |
|---|---|
| `22898fa` | **chore**：backfill_hex_celladdr.py 舊資料補救 script（WAKE_UP_TODO #9 ready，等 --apply） |
| `8e9806b` | **test**：專案層權限安全核心（assert_project_access + _PERM_LEVELS + optional auth）— +17 |
| `101474b` | **test**：write_audit 業務邏輯（safe 合約 + 欄位組裝 + payload_hash 一致性）— +9 |
| `a5eb683` | **fix**：ingest cell_addr 拒絕 hex 短碼（addr_geocode_failed 真因，**完成 WAKE_UP_TODO #7**）+7 守護測試 |

**本輪重點**：
- WAKE_UP_TODO #7 **完成**：0517test 案件 69 筆 addr_geocode_failed 真因
  是台哥大-第二類.xlsx 有 ≥2 欄都映 cell_addr（「起址」吐 hex、「基地台
  位址」吐真地址），W1.5「空值不覆蓋」紀律下 1866 列被真地址覆蓋、
  69 列真地址空 → hex 殘留。`_normalize_row` Pass 1 加 hex 短碼 guard
  攔截 6–12 字純 hex 改寫到 sector_id，coverage 改歸 cellid_only。
  既有資料未洗（不會回頭洗 DB）、未來上傳止血。
- WAKE_UP_TODO #2 **部分完成**：補了 audit（write_audit）與 security
  （assert_project_access / optional auth / anonymous admin 範本隔離）
  兩塊業務邏輯測試（+26）。剩 members API（_require_project_owner /
  revoke_member）、軟刪流程、format_reports 處理流程等。
- 全套 pytest：131 → **164**（+33）。

---

## 上輪（2026-05-17 ～ 05-23）大事記

| Commit | 內容 |
|---|---|
| `c2d7c1b` | **P7**：專案分享連結（匿名唯讀檢視；`share_links` 表 + `api/share.py` + `share.html`） |
| `7c89754` | 案件下拉選單 + 分享連結簡化（30 分鐘一鍵分享） |
| `43bb800` | docs：新增完整專案技術文件 |
| `b2a7e83` / `d4dcbe0` | 登入流程三頁背景影片改走 Cloudflare R2 CDN |
| `e55d69e` | fix：schema.sql 自足化 + 正式環境部署檢查清單 |
| `675864d` | feat：後端啟動設定安全自檢 |
| `1353b09` | **test**：P3–P7 API 契約與 auth 守衛測試（92 → **125**） |
| `7b82358` | fix：修復 bug 掃描發現的 6 項問題（map 靜默截斷 / 使用者列舉 / 權限文件等） |
| `a72e20b` | feat：Supabase 保活機制（APScheduler 每 6h ping 一次 DB） |
| `14499f1` | feat：欄名對照表顯示 code 預設 + 帳號申請改為使用者自訂密碼 |
| `c059908` | feat：全 admin 操作稽核覆蓋 + 專案軟刪 API |
| `9f13704` | feat：admin 介面分頁化 + api.js 單一來源 + XSS/401 強化 |
| `04e7100` | test：前端 UI smoke test（playwright-core 驅動系統 Chrome） |
| **本輪** | **feat：上傳定位透明化（coverage 端點 + L1 收據 + L2 banner + L3 詳細 modal）** |

---

## ⭐ 本輪重點：上傳定位透明化（2026-05-23 下午）

**動機**：同事反應「上傳 300 筆只跳 200 筆」。實測 `0517test` 案件
**9129 / 9129** 寫入但 **145 筆未定位**（76 cellid_only + 69 addr 失敗）
—— 過去使用者完全看不到這個落差，違反證據完整性原則。

**做了**：
- 後端 3 端點：`/coverage`（聚合）、`/unlocated`（加 reason 標籤 + filter）、
  `/unlocated.csv`（下載；UTF-8 BOM 給 Excel）。三類 reason 純依既有欄位
  推導，不需 migration。
- 前端三層 UI：L1 上傳完成 receipt（4 個數字 + 3 原因 + 下載鈕）、L2 地圖
  頂部常駐 banner（`without_geom > 0` 才顯示）、L3 詳細 modal（按原因
  collapsible + 每段「排除方式」說明 + 列表 + CSV）。

**驗證**：131/131 pytest（+6 契約）；對 0517test 真實案件實打三端點數字
正確；playwright 驗 L2 banner 與 L3 modal 渲染（145 / 9129、76 + 69 兩段
正確展開）。

**遺留 finding**：addr_geocode_failed 那 69 筆的 cell_addr 是 sector
代碼（`0E2921B7` 之類），是 normalize / dialect 把錯欄位塞進 cell_addr。
下次處理（待辦新增第 7 條）。

---

## 快速啟動

```bash
open -a Docker
docker compose -f infra/docker-compose.yml up -d db
cd "/Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend"
source .venv/bin/activate
uvicorn app.main:app --port 8000 &
cd ../frontend && python3 -m http.server 5501
# 登入頁：http://127.0.0.1:5501/login.html
# 主系統：http://127.0.0.1:5501/index.html
```

> ⚠️ 本機 `.env` 目前 `AUTH_ENABLED=true` —— index.html 不再自動匿名 admin，
> 需先登入取得 token。無已知密碼時可用後端金鑰自行鑄 token：
> `python -c "import app.main; from app.security import create_access_token; print(create_access_token({'sub':'CIDadmin'}))"`
> （`CIDadmin` 為目前唯一在用的 active admin 帳號）。

> 全新環境需套 **三個** migration（schema.sql 不含）：
> `migration_permissions.sql`、`migration_account_requests.sql`、
> `migration_share_links.sql`（見 CLAUDE.md 第三節）。

---

## 待辦（依優先級）

### 中期

| # | Task | 說明 |
|---|---|---|
| 1 | **填充 cell_towers 座標表** | 架構（P4.1）就緒但表是空的；向業者取得基地台座標 CSV 匯入 |
| 2 | **P3–P6 API 補自動化測試** | 已補 P3–P7 契約/守衛測試（125）+ audit + security 業務邏輯（164）；剩 members API（_require_project_owner / revoke_member / list_members）、軟刪流程、format_reports 處理流程 |
| 3 | **carrier_profile DB 同步** | 把 `_RAW2CANON` 所有 key 補進 DB `mapping_json` |

### 長期

| # | Task | 說明 |
|---|---|---|
| 4 | **檢警分艙 / 案件分艙細緻權限** | 目前 admin/user + project_members 三級已可用，尚無組織層隔離 |
| 5 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改 watchmedo |
| 6 | **前端 UI smoke test 擴充** | 已建 `frontend/tests/`（playwright-core，17 / 28 條全綠）；之後新增頁面 / 互動時補上對應 assertion |
| ~~7~~ | ~~**`addr_geocode_failed` 真因**~~ | **✅ 2026-05-24 完成（`a5eb683`）** — 真因是 ≥2 欄都映 cell_addr，hex 在真地址空時殘留；`_normalize_row` Pass 1 加 hex 短碼 guard 改寫到 sector_id。+7 守護測試。既有 DB 資料未洗（需另寫 backfill script）。 |
| **8** | **手動定位（L3 Phase 2）** | L3 modal 加「點地圖即儲存」按鈕：使用者選未定位 row → 在地圖上點 → 寫入 raw_traces.geom + audit。背景見 CLAUDE.md 第五節 N。 |
| **9** | **舊資料 hex backfill** | **🟡 2026-05-24 script 完成（`22898fa`），等使用者 --apply** — `backend/scripts/backfill_hex_celladdr.py`，DRY RUN 預設。對本機 DB 驗證 0517test 案件正好 69 列可搬（與 #7 預測完全吻合）、0 列 sector_id 已佔用。使用者本機需手動跑 `--apply` 才會實際更新 DB + 寫 audit（一支 audit per project，action='backfill.hex_celladdr'）。 |

---

## 提醒

- 本機 `.env`：`AUTH_ENABLED=true`、`GEO_OSM_FALLBACK=1`，Google API key /
  `SECRET_KEY` 已設。
- Redis 離線不致命（geocode 已全包 try-catch）；本輪 session Redis 未啟動。
- 正式 DB 為 Supabase，後端有保活機制（每 6h ping）；本機開發用 Docker PostGIS。
- 案件資料 / `Pic/` 素材已在 `.gitignore`，不會被 commit。
- 詳細架構與 onboard checklist 見 `CLAUDE.md`。
