# 醒來要做的事（v12 — P7 完成 + admin 稽核/分頁化）

> 更新時間：2026-05-23
> 狀態：✅ 125/125 pytest passed ｜ 證據完整性 9/10

---

## 本輪（2026-05-17 ～ 05-23）大事記

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

---

## ⚠️ 本輪完成、已驗證、待 commit

> 若下次開 session 時 `git log` 已含下列兩個 commit，代表已提交，本節可略過。

`git status` 有 15 個檔的未 commit 改動，是一組完整功能交付，拆兩個 commit：

### Commit 1（後端）：全 admin 操作稽核覆蓋 + 專案軟刪 API
- 9 個 admin 端點全補 `write_audit`（帳號建立/停用/啟用/改角色、申請核准/
  拒絕、欄名對照表增刪、座標表匯入/清空、格式回報處理）。`update_user` 的
  details 只記布林、絕不寫密碼明文。
- 新增 `DELETE /api/projects/{id}`：軟刪整個案件（owner/admin），失敗也寫
  `project.delete_failed` audit。
- `GET /api/projects/` 回傳 `permission` 欄、並過濾已整案軟刪的案件。

### Commit 2（前端）：admin 介面分頁化 + api.js 單一來源 + XSS/401 強化
- `api.js` 改單一來源（`window.CT_API` / `CT_API_BASE`），7 頁統一改用。
- `admin.html` 重組為三分頁（帳號 / 資料表 / 稽核），密碼重設與改角色改
  per-row 操作。
- 全面以事件委派取代 `inline onclick + JSON.stringify`，根除 onclick
  屬性注入面（index/admin 多處 latent XSS）。
- 新增 `apiFetch` / `handle401` 集中處理 token 過期。
- `index.html` 下拉新增刪除專案鈕；`share.html` 改 30 秒倒數。
- 刪除已不再使用的 `index.backup.html`。

### 驗證結果（2026-05-23）
- 後端：125/125 pytest；真實 admin token 端對端驗證 `GET /projects/`
  permission 欄、全軟刪案件排除、`DELETE` 404/200 路徑 + audit 寫入。
- 前端：playwright-core 驅動系統 Chrome 實跑 7 頁，admin 三分頁切換 +
  各分頁資料載入正常、無 pageError；audit 查詢、share 倒數皆正常。

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
| 2 | **P3–P6 API 補自動化測試** | 已補 P3–P7 契約/守衛測試（125）；ingest 以外的「業務邏輯」層仍偏薄 |
| 3 | **carrier_profile DB 同步** | 把 `_RAW2CANON` 所有 key 補進 DB `mapping_json` |

### 長期

| # | Task | 說明 |
|---|---|---|
| 4 | **檢警分艙 / 案件分艙細緻權限** | 目前 admin/user + project_members 三級已可用，尚無組織層隔離 |
| 5 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改 watchmedo |
| 6 | **前端 UI 自動化回歸** | P6 後 index.html 近乎重寫、admin.html 本輪重排；目前靠人工 / playwright 臨時腳本測試 |

---

## 提醒

- 本機 `.env`：`AUTH_ENABLED=true`、`GEO_OSM_FALLBACK=1`，Google API key /
  `SECRET_KEY` 已設。
- Redis 離線不致命（geocode 已全包 try-catch）；本輪 session Redis 未啟動。
- 正式 DB 為 Supabase，後端有保活機制（每 6h ping）；本機開發用 Docker PostGIS。
- 案件資料 / `Pic/` 素材已在 `.gitignore`，不會被 commit。
- 詳細架構與 onboard checklist 見 `CLAUDE.md`。
