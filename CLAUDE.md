# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 最近一次更新：2026-05-18（P7 分享連結 commit；本檔由 /init 重新對齊 git log）

---

## 一、專案是什麼

**CellTrail**：刑事偵查用的「基地台連線歷程」匯入 + 視覺化系統。

**使用者**：刑事警察（Project owner: 高市刑大冠鈞 chen95572295@gmail.com / e43691@kcg.gov.tw）。

**核心流程**：
1. 偵查員拿到電信業者交付的歷程檔（CSV/Excel/PDF）
2. 上傳到系統 → 後端解析 → DB（或「臨時查看」模式不寫 DB）
3. 地圖視覺化：基地台位置 + 精度圓 + 方位角扇形（Google Maps 風格全螢幕 UI）
4. 證據完整性（前身為「法庭可防禦性」，P6 改用較中性的措辭）：軟刪、audit_logs、SHA-256 證據鏈、方位角北方基準標註

**證據完整性等級**：4/10 → **9/10**（P0+P1+P2+P2.5+P2.8 已完成；含方位角基準 dashboard 與報告地圖截圖）。

---

## 二、技術棧

| 層 | 工具 |
|---|---|
| 後端 | FastAPI + uvicorn (port 8000)；slowapi 速率限制；python-jose JWT |
| DB | PostGIS 16 (Docker, port 5432) — `postgis/postgis:16-3.4` |
| Cache | Redis（**選配**；stats/hit dedup 與 geocode cache 失效但不致命，已全面包 try-catch） |
| 前端 | 純 HTML/JS/Leaflet + markercluster（無 build step），靜態檔案 port 5501 |
| Python | 3.13.2（homebrew），venv 在 `backend/.venv` |
| Repo | https://github.com/ChenJun43691/celltrail |

**本機路徑**：`~/Desktop/Python程序開發/CellTrail`（含中文路徑，命令注意 quote）。

**`.env` 重點**：
- `AUTH_ENABLED=false`（本機開發 anonymous admin），production 必須改 `true`
- `GEO_OSM_FALLBACK=1` + `NOMINATIM_EMAIL=...`（OSM 備援已啟用）
- `GOOGLE_MAPS_API_KEY`、`SECRET_KEY`（JWT 簽章，production 必須改）

---

## 三、常用命令

### 啟動基礎設施

```bash
# 啟動 PostGIS（Redis 選配）
docker compose -f infra/docker-compose.yml up -d db
docker compose -f infra/docker-compose.yml up -d redis   # 需要時

# 套用 DB schema（冪等，IF NOT EXISTS，重跑安全）
# 等 docker ps 顯示 (healthy) 再跑
docker exec -i celltrail_db psql -U celltrail -d celltrail < backend/app/db/schema.sql
# 或用腳本（含健康等待）：
bash backend/scripts/apply_schema_p0p1.sh

# schema.sql 不含的 migration（皆冪等，重跑安全）
docker exec -i celltrail_db psql -U celltrail -d celltrail < backend/app/db/migration_permissions.sql       # P3 project_members
docker exec -i celltrail_db psql -U celltrail -d celltrail < backend/app/db/migration_account_requests.sql  # P3 account_requests
docker exec -i celltrail_db psql -U celltrail -d celltrail < backend/app/db/migration_share_links.sql       # P7 share_links
```

### 啟動後端

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --port 8000
# Swagger UI：http://localhost:8000/api/docs
```

> `--reload` 在 Python 3.13 macOS 有 spawn bug（task #27），建議不加。

### 啟動前端

```bash
cd frontend
python3 -m http.server 5501
# 登入頁：http://127.0.0.1:5501/login.html
# 主系統：http://127.0.0.1:5501/index.html（AUTH_ENABLED=false 自動匿名 admin）
```

### 執行測試

```bash
cd backend
source .venv/bin/activate

pytest app/tests/ -v                                      # 全部（92 passed）
pytest app/tests/test_ingest_match_col_idx.py -v          # 單一測試檔
pytest app/tests/test_audit.py::test_write_audit_fields -v # 單一測試函式
```

> smoke tests 不依賴 DB / Redis / Google，CI 可直接執行。
> **測試涵蓋範圍偏 ingest 核心**（normalize / dialect / compound split / match_col_idx / audit / evidence / azimuth_ref）。P3–P6 的 API（auth、members、parse-only、format-reports、cell-towers）目前**靠手動驗證**，無自動化測試。

### 端對端 smoke test（需 DB + uvicorn 已啟）

```bash
bash backend/scripts/smoke_audit.sh   # 最後印綠色 ✓ 表示 audit chain 完整
bash backend/scripts/smoke_upload.sh
```

### venv 救援（套件損壞時）

```bash
cd backend
bash scripts/rebuild_venv.sh   # 核彈級重建，約 5 分鐘
```

---

## 四、Milestone 進度（依 commit 時間軸）

### 已完成（git log 由舊到新）

| Milestone | 重點成果 |
|---|---|
| **W2.1–W2.6** | multi-sheet、buried header、compound split、中華上網方言、`_match_col_idx` 兩階段、雙向通聯 merged-header；所有真實樣本達 100% 或資料物理上限 |
| **P2.5-A/B** | azimuth_ref 北方基準標註：popup 紅字警告 + 標註 modal（select 三選一 + evidence ≥5字）+ PATCH 流程 |
| **P2.5-C** | 方位角基準標註狀態 dashboard（unknown 比例 / 最近標註人 / audit trail viewer） |
| **P2.6** | popup polish：`azimuth=null` 不顯示空殼、`cell_id` 為空顯示「—」、`azimuth=0` 邊界 |
| **P2.7** | 臨時查看 vs 儲存為專案 + 新手導覽 + admin 稽核日誌 |
| **P2.8** | 報告 PDF 含地圖截圖（OSM 靜態圖磚後端合成，`services/staticmap.py`） |
| **P3** | 帳號權限系統：後端完整（auth/JWT、project_members、account_requests）+ 前端登入/管理 UI + 帳號申請流程 + 資安強化 |
| **P4.1** | 本地基地台座標對照表 `cell_towers`（cell_id → lat/lng，geocode 前置查詢） |
| **P4.2** | 地圖「顯示定位時間」標籤（zoom 響應、per-series 切換） |
| **P4.3** | 臨時使用 vs 專案管理模式切換系統（parse-temp API、案件名稱下拉自動補全） |
| **P4.4** | login / register / change-password 全面改版（影片背景、毛玻璃卡、深藍科技風） |
| **P4.5** | 欄名對照表管理 UI（carrier_profile admin API + admin.html） |
| **P5** | 訪客免登入流程 + parse-only API（只解析、不寫 DB） |
| **P6** | Google Maps 風格 UI 改造 + 格式回報三層機制 + geocode 批次優化（見下節五-H/I/J） |
| **P7** | 專案分享連結：12 小時臨時免登入唯讀檢視（`share_links` 表 + `api/share.py` + `frontend/share.html`）；見下節五-M。同 commit 另含 index.html「回到先前的專案」下拉、measure 距離標籤改用 leaflet tooltip、admin 核准流程顯示順序修正、register 帳號 placeholder 文案調整 |

### 各真實樣本當前 normalize 通過率

| 樣本 | 通過率 | 備註 |
|---|---|---|
| 彭奕翔 `0801-0903...xlsx` | 100% | W2.3 後 |
| `網路歷程.xltx` / `網路歷程-2a0c1c9a.xltx` | 100% | W1.5 baseline |
| `電話通聯+歷程.xlsx` | 60.31% | 剩餘為**物理失敗**（798 列無 cid + 362 列無 ts + 30 noise）|
| `周蔓達上網歷程.xlsx` | 51.08% | 達**資料物理上限**（47.35% 列原始就缺起台/起址）；6769 列 demo_case |
| `楊云豪…(含方位角).pdf` | 100% + sector_id 100% 正確 | W2.5 |

**結論**：手邊所有真實樣本要嘛 100%、要嘛達資料物理上限。**ingest pipeline 沒有已知未修的 silent bug**。

---

## 五、關鍵設計決策（為什麼這樣做）

### A. 兩層方言系統（W2.4）

**問題**：中華上網方言中 `起台 → start_ts`、`起址 → cell_id`，但 W1 既有 `_RAW2CANON` 把 `起台 → cell_id`（錯）。直接修 W1 會壞掉 header detection scoring（用 `_RAW2CANON` 投票）。

**解法**：
- 全域 `_RAW2CANON` 保留為「header detection signal」（決定哪行是 header，不負責正確性）
- `_DIALECT_HEADER_MAPS` 為「actual normalize rule」（dialect-specific override，正確映射）
- `_iter_rows_excel` per-sheet 跑 `_detect_dialect`，命中後在每個 row 注入 reserved key `__celltrail_dialect__`，`_normalize_row` 自動消化 tag

### B. dialect 偵測雙訊號（W2.4）

避免誤判混合 sheet：必須**同時**滿足 ① headers 含 `{起台, 起址}` 雙指紋 ② ≥50% sample row 的「通話類別」含「上網」。設計上故意保守拒絕。

### C. _match_col_idx 兩階段（W2.5）

**Bug**：PDF header 含「細胞名稱」+「細胞」並存時，鬆散 `c in name` 比對會在「細胞名稱」上提早命中 → sector / cid 撞同一 index → silent corruption。

**解法**：Pass 1 精確匹配（canon equal）認領 index；Pass 2 子字串備援，跳過 Pass 1 已認領的 index。保留異體字（臺/台）與寬鬆欄名容錯。

### D. azimuth_ref 設計（P2.5）

**法庭背景**：電信業者 azimuth 的「北方基準」沒有統一規格（磁北/真北）。台灣高雄區磁偏角約 -4°~-5°，500m 距離下差出約 50m，足以差出整條街。

**設計**：DB 預設 `'unknown'`（不擅自推論）；PATCH `/api/projects/{p}/targets/{t}/azimuth-ref` 由 admin 標註 `magnetic`/`true`/`unknown`；evidence ≥5 字必填；audit_logs 自動記錄誰、何時、依何書面證據。

### E. popup 條件渲染（P2.6）

- `p.azimuth != null` 而非 truthy check（避免 azimuth=0「正北」被誤過濾）
- cell_id 為空顯示「—」；azimuth_ref 行只在有 azimuth 時顯示

### F. _parse_ts ISO 8601 補強（W2.4-pre）

新增 `%Y-%m-%dT%H:%M:%S.%f` 與 `%Y-%m-%dT%H:%M:%S`。**故意拒絕** `Z` 與 `+08:00` 後綴：系統假設全為 naïve 台北時間，由 `_parse_ts` 統一加 `TPE_TZ`。

### G. 帳號權限系統（P3）

- **全域角色**（`users.role`）：`admin` / `user` 兩級
- **專案層權限**（`project_members.permission`）：`owner` / `collaborator` / `viewer` 三級；`assert_project_access(user, project_id, min_permission)` 為共用守衛
- **帳號申請流程**：`account_requests` 表（`pending`/`approved`/`rejected`），訪客送申請 → admin 在 `/api/account-requests` 核准建立帳號
- JWT 12 小時效期（一個工作天）；`get_current_user_optional` 支援 `AUTH_ENABLED=false` 的匿名 admin fallback
- 關鍵檔：`backend/app/security.py`（JWT + 守衛 + 權限），`api/auth.py` `api/members.py` `api/requests.py` `api/users.py`

### H. 兩階段 ingest 重構（P6）

**問題**：大檔上傳逐筆序列 geocode，每筆吃一次 Redis/Google round-trip（周蔓達 6769 筆 → 6 分 43 秒）。

**解法**：`_parse_rows_to_records` / `_parse_pdf_to_records` 改三 phase：
1. **phase1**：normalize + 時間驗證（核心 `_normalize_row` / `_match_col_idx` **未動**，W2 通過率不受影響）
2. **phase2**：收集 unique `(cell_id, cell_addr)` → `geocode.lookup_bulk`（一次 SQL `ANY` + 一次 Redis `MGET`，剩餘 miss 才逐筆打 Google）
3. **phase3**：組裝 records

實測 403s → 2.5s（redis_hit 3704、phase2 僅 34ms）。每個 phase 有 `perf_counter` 計時 log。

### I. 格式回報三層機制（P6）

電信業者格式眾多，遇不支援格式時三層處理：
1. **解析失敗回 422 + diagnosis**：`ParseDiagnosisError`，以 `_peek_headers` + `_build_diagnosis` 推斷缺哪些欄位
2. **手動欄位對應**：前端 mapping override → `_apply_user_mapping` 把使用者選的欄 rename 成 `_RAW2CANON` 已知 alias 再 normalize
3. **回報 API**：`format_reports` 表 + `api/format_reports.py`（POST 回報 / GET 列表 / PATCH 處理；只存 headers + diagnosis，不存原始檔內容）

### J. parse-only / parse-temp（P5 / P4.3）

- `POST /api/parse-only`：純解析、**不寫 DB**，回 records 供訪客免登入預覽（記憶體內）
- `POST /api/upload/parse-temp`：「臨時查看」模式，解析後給前端但不落 raw_traces
- `POST /api/upload`：正式模式，寫 DB + evidence 證據鏈
- 設計用意：偵查員可先「看看這檔能不能解」再決定是否建案

### K. cell_towers 本地座標表（P4.1）

geocode 前置查詢：`_lookup_from_local(cell_id, addr)` 先查 `cell_towers`（`cell_id` UNIQUE），命中即直接用，不打 Google/OSM。`ON CONFLICT(cell_id) DO UPDATE` 冪等。admin 可由 `api/cell_towers.py` 匯入 CSV。**目前表是空的** — 需向業者取得座標表填入。

### L. 關鍵依賴版本約束

- **bcrypt 必須固定 `==4.2.1`**：passlib 1.7.4 與 bcrypt 5.x 不相容（"password cannot be longer than 72 bytes"）。
- **psycopg 所有 `cur.execute()` 必須帶 `prepare=False`**：connection pooler 不支援 server-side prepared statements。
- **pytest 固定 `==8.3.3`**：pytest 9 與 anyio/fastapi 偶有相容性問題。

### M. 分享連結（P7）

**需求**：偵查員想把某專案地圖給「沒有帳號」的同仁／長官看一眼。

**安全模型**：
- 「持連結即可免登入檢視」；唯一防線是 token 不可猜測 — `secrets.token_urlsafe(24)`（≈192-bit 熵）。
- **純檢視**：公開端點 `GET /api/share/{token}` 只回地圖 GeoJSON，無任何寫入／報告下載入口。
- 效期固定 12 小時，**後端寫死**不開放呼叫端自訂（避免有人開出永久連結）；owner 可在到期前 `DELETE` 撤銷（外流補救）。
- 連結有效 = `revoked_at IS NULL AND expires_at > now()`；失效回 **410 Gone**、不存在回 404。
- 每次成功檢視 `use_count+1` 並寫一筆 `audit_logs`（`action=share_link.view`，含 IP）。
- 建立／撤銷需該專案 **owner 或系統 admin**（`_require_project_owner`）；`created_by` 對匿名 admin（id=0）寫 NULL。

**刻意決策**：`share.py` 的 `_fetch_map_geojson` **複製** `/map-layers` 的查詢而非 import map.py — 讓 share 模組自足、不牽動既有 API。前端 `frontend/share.html` 是獨立精簡頁（重畫 sector/精度圓，不依賴 index.html），`<meta robots=noindex>` 避免被索引。

---

## 六、待辦事項（依優先級）

### 中期

| # | Task | 說明 |
|---|---|---|
| 1 | **填充 cell_towers 座標表** | 架構（P4.1）已就緒但表是空的；向業者取得基地台座標 CSV 匯入，可徹底解決純數字 cell_id 的 geocode 問題 |
| 2 | **P3–P6 API 補自動化測試** | auth / members / parse-only / format-reports / cell-towers 目前只有手動驗證 |
| 3 | **carrier_profile DB 同步** | 把 `_RAW2CANON` 所有 key 補進 DB `mapping_json`（讓 DB 真正成為 SoT） |

### 長期

| # | Task | 說明 |
|---|---|---|
| 4 | **檢警分艙 / 案件分艙細緻權限** | 目前 admin/user + project_members 三級已可用，但尚無組織層隔離 |
| 5 | **task #27 uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改 watchmedo |
| 6 | **前端 UI 自動化回歸** | P6 後 index.html 近乎重寫，目前 UI 互動只靠人工測試 |

---

## 七、已知問題與環境陷阱

### 1. geocode 全敗會讓地圖空白

`/map-layers` 的 SQL 有 `WHERE geom IS NOT NULL`，geocode 失敗的列被過濾掉 → 地圖 0 點、series 列表空。確認：

```sql
SELECT count(*) AS total, count(geom) AS with_geom
FROM raw_traces WHERE project_id=? AND deleted_at IS NULL;
```

OSM 備援預設已啟用（`.env` `GEO_OSM_FALLBACK=1`）。

### 2. Redis 沒跑不致命

`geocode.py` 的 `rds.get/setex` 已全包 try-catch；Redis 離線只是 cache miss + stats/hit dedup 失效。要修：`docker compose -f infra/docker-compose.yml up -d redis`。

### 3. macOS Docker daemon 不會自動啟動

每次重開機要 `open -a Docker`，等 menu bar 鯨魚 icon 穩定再跑 `docker compose up -d db`。

### 4. apply_schema timing 陷阱

容器才 `health: starting` 就跑 schema 腳本會失敗（unix socket 還沒建好）。要先 `docker compose ps` 確認 `(healthy)`。schema 是 idempotent，重跑安全。

### 5. PDF / 大檔 ingest 慢

P6 批次 geocode 後已大幅改善；首次解析（cache 全 miss）仍受 Google/OSM 節流限制。`上傳中…` toast 期間請耐心等，不要重複點。

### 6. 含中文路徑

repo 路徑 `~/Desktop/Python程序開發/CellTrail`、樣本檔名含中文括號全形空格。shell 命令必須 quote，curl `@filepath` 要反斜線 escape。

### 7. 有三張表不在 schema.sql

`project_members`（P3）、`account_requests`（P3）、`share_links`（P7）分別在
`migration_permissions.sql` / `migration_account_requests.sql` / `migration_share_links.sql`，
新環境要逐一另外套（見第三節）。

---

## 八、git commit message 風格

依使用者偏好（精確、深入、邏輯嚴密、繁體中文）：

```
<milestone-id>: <短摘要英文>

Background:
- 用條列說明問題與根因（含「為什麼會發生」）
- 嚴格區分「事實／推論／結論」

Fix (簡述方案類型):
- 用條列描述每個修法決策的理由（「為什麼這樣做」優於「做了什麼」）

Verification:
- sandbox 驗證的 case 數量 + 涵蓋邊界
- 真實樣本回歸通過率

Risk: <0/低/中/高> + 說明為什麼

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 九、與 Claude 工作的偏好

使用者明確要求（見 `<user_preferences>`）：

- **語言**：繁體中文溝通
- **品質**：精確度、深度、嚴密邏輯推理
- **嚴格區分**：「已確認的事實」/「推論」/「結論」
- **不確定就說不確定**，**禁止無據猜測**
- **教學風格**：「大學教授」耐心解釋「為什麼這樣做」的底層原理
- **類比**：歡迎使用，但要嚴謹、結合實際案例
- **領域敏感**：刑事偵查、法律、技術開發等議題優先確保事實準確性與證據可靠性

工作流模式：
- 使用者常說「繼續」「依你判斷」 → 沿用我的推薦
- 但**大改動前**（>1 小時 / 涉及 modal/UI/migration）建議先停下來確認方向
- commit 命令給使用者本地跑（sandbox 沒 git push 認證）
- 純前端 / 邏輯改動可以連跑多個 commit，DB migration 必須使用者手動

---

## 十、快速 onboard 檢查清單

下次開新 session，先做這幾件：

1. `git log --oneline -10` 看最近 commit
2. `cat WAKE_UP_TODO.md` 看上輪結尾留下的待辦
3. 看 `infra/docker-compose.yml` 確認 DB/redis 是否需要先啟動
4. 看 `backend/app/main.py` 的 router 清單（最快掌握 API 全貌）
5. 看 `backend/app/services/ingest.py` 的 `_DIALECT_HEADER_MAPS` 與三 phase 函式（`_parse_rows_to_records` / `_parse_pdf_to_records`）
6. 看 `backend/app/db/schema.sql` + 三個 migration 檔（permissions / account_requests / share_links）
7. 前端關鍵函式改用 grep 定位（P6 後 index.html 已重寫，行號不可靠）：`openAzRefModal`、`refreshSeriesPanel`、popup 渲染、格式診斷 modal

關鍵檔案地圖：

```
backend/app/
  main.py            ← FastAPI 入口 + 全部 router 掛載清單
  security.py        ← JWT、get_current_user、require_admin、assert_project_access、project_members
  services/
    ingest.py        ← 核心邏輯（_normalize_row, _match_col_idx, _detect_dialect, _parse_ts,
                        三 phase ingest, ParseDiagnosisError, _peek_headers, _apply_user_mapping）
    geocode.py       ← Google + OSM 備援 + cell_towers 本地查詢 + lookup_bulk 批次
    audit.py         ← write_audit() helper
    evidence.py      ← SHA-256 落地
    carrier_profile.py
    report.py        ← evidence-report PDF 組裝
    staticmap.py     ← OSM 靜態圖磚合成（報告地圖截圖）
    limiter.py       ← slowapi rate limiter 實例
  api/
    health / auth / users / upload / map / targets / stats / geocode / audit / report
    members          ← /api/projects/{id}/members 專案成員權限
    requests         ← /api/account-requests 帳號申請審核
    cell_towers      ← /api/cell-towers stats/import/delete
    carrier_profile  ← 欄名對照表管理
    parse_only       ← /api/parse-only（純解析不寫 DB）
    format_reports   ← /api/format-reports 格式回報
    share            ← /api/share（P7 分享連結；公開端點 GET /api/share/{token}）
  db/
    schema.sql                    ← raw_traces / users / audit_logs / evidence_files /
                                     carrier_profiles / cell_towers / format_reports
    migration_permissions.sql     ← project_members（P3，需另外套）
    migration_account_requests.sql← account_requests（P3，需另外套）
    migration_share_links.sql     ← share_links（P7，需另外套）
    session.py                    ← psycopg pool

frontend/
  index.html         ← 主頁（P6 Google Maps 風格全螢幕 UI；含 popup、azimuth modal、
                        格式診斷 modal、measure、自訂標記、新手導覽、P7 分享連結 modal）
  share.html         ← P7 分享連結公開檢視頁（獨立精簡頁，憑 ?t=token 唯讀檢視）
  login.html / register.html / change-password.html  ← 帳號流程（P4.4 深藍科技風）
  admin.html         ← 管理：使用者 / 欄名對照表 / 格式回報
  audit.html         ← P2 稽核檢視
  api.js             ← 前端 API base/helper

infra/docker-compose.yml   ← db + redis + tileserver
```
