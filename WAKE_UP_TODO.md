# 醒來要做的事（v11 — P5 / P6 完成）

> 更新時間：2026-05-17
> 狀態：✅ 92/92 pytest passed ｜ 證據完整性 9/10

---

## 本輪（2026-05-11 ～ 05-17）大事記

| Commit | 內容 |
|---|---|
| `d262df9` | refactor: 使用者列精簡 + 法庭防禦移入案件區塊 + 文字語系統一 |
| `e8e628f` | **P5**: 訪客免登入流程 + parse-only API（純解析、不寫 DB） |
| `4dec36c` | refactor: 新手導覽步驟重整（移除「填入案件代號」，加入「地圖顯示結果」） |
| `d3ad73a` | **P6**: Google Maps 風格 UI 改造 + 格式回報三層機制 + geocode 批次優化 |
| `5e5b9bb` | P6-fix: PDF 診斷 modal 隱藏「手動對應」按鈕 |
| `56029b9` | chore: .gitignore 加入 Pic/ |

### P5 / P6 功能摘要

| 功能 | 說明 |
|---|---|
| **P5 訪客免登入** | `POST /api/parse-only` 純解析回 records，不寫 DB；訪客可先預覽再決定建案 |
| **P6 UI 改造** | 全螢幕地圖 + 漢堡側滑選單 + 右下浮動按鈕；測量距離、自訂標記、markercluster |
| **P6 格式回報三層** | ① 解析失敗回 422 + diagnosis ② 手動欄位對應 ③ `/api/format-reports` 回報 + admin 處理 |
| **P6 geocode 批次化** | `lookup_bulk`：unique key 一次 SQL `ANY` + Redis `MGET`；ingest 改三 phase。403s → 2.5s |
| **P6 韌性強化** | 全域 error/unhandledrejection 捕捉、Redis 容錯、azimuth `null` 嚴格判斷 |

---

## ⚠️ 尚未 commit 的工作

`git status` 有三個前端檔的未 commit 改動，下次 session 先確認是否要保留：

```
M frontend/admin.html
M frontend/index.html
M frontend/register.html
```

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
# 主系統：http://127.0.0.1:5501/index.html（AUTH_ENABLED=false 自動匿名 admin）
```

> 全新環境另需套兩個 migration（schema.sql 不含）：
> `migration_permissions.sql`、`migration_account_requests.sql`（見 CLAUDE.md 第三節）。

---

## 待辦（依優先級）

### 中期

| # | Task | 說明 |
|---|---|---|
| 1 | **填充 cell_towers 座標表** | 架構（P4.1）就緒但表是空的；向業者取得基地台座標 CSV 匯入 |
| 2 | **P3–P6 API 補自動化測試** | auth / members / parse-only / format-reports / cell-towers 目前只有手動驗證 |
| 3 | **carrier_profile DB 同步** | 把 `_RAW2CANON` 所有 key 補進 DB `mapping_json` |

### 長期

| # | Task | 說明 |
|---|---|---|
| 4 | **檢警分艙 / 案件分艙細緻權限** | 目前 admin/user + project_members 三級已可用，尚無組織層隔離 |
| 5 | **uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改 watchmedo |
| 6 | **前端 UI 自動化回歸** | P6 後 index.html 近乎重寫，目前 UI 只靠人工測試 |

---

## 提醒

- 本機 `.env`：`AUTH_ENABLED=false`、`GEO_OSM_FALLBACK=1`、Google API key / `SECRET_KEY` 已設。
- Redis 離線不致命（geocode 已全包 try-catch）。
- 案件資料 / `Pic/` 素材已在 `.gitignore`，不會被 commit。
- 詳細架構與 onboard checklist 見 `CLAUDE.md`（已於 2026-05-17 同步更新至 P6）。
