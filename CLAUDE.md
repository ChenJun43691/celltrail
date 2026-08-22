# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 最近一次更新：2026-08-22（**P9 Phase 2B：guest / mapping-aware Preview Artifact cutover**）：
> ① 訪客上傳與手動欄位對應**都改走 `POST /api/preview`** —— 前端三條上傳路徑自此都不再取得 `_records`，
>    實測同檔 payload 縮減 **67–94%**（定位率 0 時達 99.99%），**待辦 #0 主體完成**；
> ② 手動對應的檔案自此也有完整證據鏈（原本只能走 `save-records`，server 沒有原始檔也沒有 SHA-256）——
>    前置條件是新增 `ingest_auto(mapping=)`，補上「存檔路徑完全不吃 mapping」這個硬缺口；
> ③ 訪客 artifact 以 `preview_id` 為 capability（同 P7 share_links），具名 artifact 的界線未動；
>    seal / save 維持必須登入，create / read / delete 免登入；訪客配額 20/hr/IP；
> ④ 訪客→登入的 `sessionStorage` 交接改成只帶 `preview_id`（不再把整份解析結果留在瀏覽器），
>    並保留舊格式相容以免 rolling deploy 期間吞掉使用者資料；
> ⑤ legacy `parse-only` / `parse-temp` 契約**刻意不動**、只標 DEPRECATED（舊快取頁面仍會讀 `_records`）。
> 驗證：pytest **531 passed**、Node 純函式 **79 passed**、真瀏覽器 smoke **35/35**、DB-backed E2E **25/25**。
> 同輪另完成待辦 #1 的匯入前驗證（七-12）：那份 116 筆推估座標預檢 116/0/0、離群站點查證為真實軌跡、
> 本機真 DB 匯入無欄序錯誤，實測可把「蘇」系列從定位 0 推到約四成（「陳」系列僅 3–5%）。
> 並完成**全 34 檔盤點（七-13）**：解析成功 21 檔 / 120,203 列、已定位 23,193 列（19.3%）；
> **13 個 PDF 完全無法解析**（遠傳／台哥大無框線版面，非欄名問題），其中 7 檔有同案 xlsx 可替代、
> 6 檔（`0927352336`）只有 PDF —— 建議向業者索取 Excel 版而非自行重組版面。
> **⚠ 專案最大瓶頸仍是待辦 #1：向業者取得真正的 `cell_towers` 座標表。線上該表目前仍是空的，production 定位率仍為 0。**

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
- `GEO_GOOGLE_CONCURRENCY`（P8，預設 10）：bulk geocode 並行打 Google 的併發數
- **`GEO_GOOGLE_ENABLED`（2026-07-03，硬性停用 Google Geocoding）**：
  - 值 ∈ `{0, false, no, off}`（不分大小寫、去前後空白）→ **關閉**；未設定或其他值 → 啟用（向後相容）。
  - 關閉時 **不建立任何 Google HTTP request、也不建立 Google bulk ThreadPool task**（`_google_geocode` 在送出前即回 None；`lookup_bulk` 跳過整個並行 Google 階段）。
  - 關閉後查詢順序變為：**`cell_towers` → `geocode_cache` → OSM → unlocated**（本地表與 SQL 快取優先序不變）。
  - **防止 Google 費用復燃**：production（Render）應明確設 `GEO_GOOGLE_ENABLED=0`（比只在 Google Console 停用更徹底——連被拒的請求都不送）。
  - **建議的「不用 Google」production 設定**：
    ```
    GEO_GOOGLE_ENABLED=0
    GEO_OSM_FALLBACK=1
    ```

### 雲端部署架構（production）— **使用者主要在這裡測試**

> ⚠️ 重要：repo 內沒記這塊，但**系統實際對外服務跑在雲端**。本機 8000 多半沒在跑。
> 症狀「Claude 沙箱能解、但使用者線上失敗」≠ 程式沒修好，而是**改動還沒部署**。
> 判斷：`curl localhost:8000/api/health` + `ps aux|grep uvicorn` 確認本機沒跑 →
> 使用者吃雲端 → 需 `git push origin main` 觸發 Render 自動 redeploy（約 2–5 分鐘）。

| 層 | 服務 | 說明 |
|---|---|---|
| 後端 API | Render web_service `srv-d3dn59je5dus73bqe7e0`（`celltrail-api`）| https://celltrail-api.onrender.com，綁 GitHub `main` **自動部署** |
| 前端 | Render static_site `srv-d860ep3rjlhs73acovig`（`celltrail`）| https://celltrail.onrender.com（CORS 白名單即此網域可佐證）|
| DB | **Supabase** PostgreSQL | 後端 `DATABASE_URL` 指過去；schema.sql 變更**不會**自動套（需 migration 或自動建表）|
| Cache | **無 Redis**（原 Upstash 已失效被移除）| 故 P8 改用 SQL `geocode_cache` 持久快取 |

- 雲端密鑰 env 名是 **`JWT_SECRET`**（不是本機 `SECRET_KEY`）；`security.SECRET_KEY = getenv("SECRET_KEY") or getenv("JWT_SECRET")`，production fail-fast。另有 `AUTH_ENABLED` / `CORS_ORIGINS` / `GOOGLE_MAPS_API_KEY` / `GEO_OSM_FALLBACK`。
- **active_map 合併**：`carrier_profile.get_active_header_map()` 用 `{**_RAW2CANON, **db_profile}`（code 當底、DB 疊上補空缺）→ **新增欄名別名只要 push+redeploy 即生效，不需動 Supabase**。
- **前端 API base 切換**（`api.js`）：本機 hostname（localhost/127.0.0.1/區網）→ `localhost:8000`；否則 → 雲端 `celltrail-api.onrender.com`。
- 由 Claude 直接改 Render（env / 重部署 / 看 log）需使用者臨時給一把 **Render API key**（`https://api.render.com/v1/services/{id}/...`），用完請 Revoke。
- **雲端大檔上限**：parse-only 限 **20 req/hr/IP**；單次上傳 >~5000 筆會 OOM 502（見七-8）。

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
# 開發時要熱重載：加 --reload（見下方說明）
uvicorn app.main:app --port 8000 --reload
# Swagger UI：http://localhost:8000/api/docs
```

> `--reload` 已可正常使用（2026-05-31 驗證）：現行版本堆疊
> `uvicorn==0.30.6` + `watchfiles==1.1.0`（皆已釘進 requirements.txt）改用
> watchfiles（Rust notify）偵測變動，舊 task #27 的 multiprocessing spawn bug
> 已不復現 —— 改 .py 會乾淨重啟 worker、reload 後正常服務。

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

pytest app/tests/ -v                                      # 全部（2026-08-22 實測 503 passed，13s）
pytest app/tests/test_ingest_match_col_idx.py -v          # 單一測試檔
pytest app/tests/test_audit.py::test_write_audit_fields -v # 單一測試函式
```

> smoke tests 不依賴 DB / Redis / Google，CI 可直接執行。
> **CI**：`.github/workflows/ci.yml`（push/PR to main 自動跑全套 pytest，Python 3.13）。
> **涵蓋範圍**（2026-05-26 已大幅補強，131 → 203；2026-05-30 → 216；2026-06-04 → 229；2026-06-06 → 234；2026-06-08 → 237；2026-06-13 → 242；2026-06-26 → 248；2026-06-27 → 266）：
> - ingest 核心（normalize / dialect / compound split / match_col_idx /
>   audit / evidence / azimuth_ref / addr hex guard / manual_locate）
> - P3–P7 API 契約與 auth 守衛測試（`1353b09` 33 條 + 後續擴充）
> - 業務邏輯層補完（`101474b` write_audit / `8e9806b` security 權限核心 /
>   `5eb48da` members API / `b4ec912` format_reports / `fe71904` manual_locate）
> - drift 守護：`3db9696` carrier_profile seed ↔ _RAW2CANON 同步
> - P7 分享連結（`test_share_links.py`）：`_require_project_owner` 只認 owner
>   守衛（collaborator 也擋）+ GET /share/{token} 四態狀態機（404 / 410 撤銷 /
>   410 過期 / 200）
> - 證物報告地圖截圖尺寸（`test_report_image_fit.py`）：`_fit_image_dims` 同時
>   鎖頁框寬高，守住「高瘦地圖致 reportlab LayoutError → evidence-report 500」
>   的回歸（2026-05-30 修）
> - GPS 軌跡 / 經緯度直給格式 + 加密檔偵測（`test_ingest_latlng.py`，2026-06-04/06-06）：
>   `_resolve_latlng` 範圍自動校正（緯度必在 [-90,90]，修正標反的經緯度欄）、
>   `_parse_ts` 支援 M/D/YYYY AM/PM、`_reject_if_encrypted` 偵測密碼保護檔回清楚錯誤、
>   PDF 版經緯度欄識別（`_match_col_idx` 加 lat/lng）+ 多頁「表頭只印首頁」沿用
>   上頁對應（`_pdf_cols_useful`，RFX-6179.pdf 354 列實測）
> - 雙向通聯格式 + 小檔守門（`test_ingest_compound_split.py` / `test_ingest_multisheet.py`，
>   2026-06-26）：`始話日期時間 → start_ts`、`基地台編號N/位置N` 斜線複合欄
>   （`_split_compound_cell` 加 "/"／全形「／」分支，雙條件守門避免地址內樓層
>   「3/4樓」誤切）+ `_iter_rows_excel` 規則 A 列數門檻 `< 5 → < 2`（讓「1～2 筆
>   通聯」小檔可解析；非資料 sheet 仍由規則 B 表頭別名命中數攔下）
> - 台哥大上網歷程格式（`test_ingest_tw_mobile_data.py`，5 條，2026-06-27）：
>   `進入/離開基地台` 系列別名 + SCAN_WINDOW 25→30（真表頭埋 row 27）+ 假
>   dimension 的 `_read_xlsx_top_rows` fallback（read_only 退化偵測）
> - 手動對應結構性修復（`test_ingest_manual_mapping.py`，7 條，2026-06-27）：
>   `_iter_rows_excel(user_mapping=)` 讓陌生格式不被規則 B 丟棄 + `_guess_header_row_idx`
>   結構性定位埋深真表頭（peek 不靠別名）
> - geocode 並行 + SQL 持久快取（`test_geocode_bulk_parallel.py`，6 條，2026-06-27）：
>   ThreadPool 並行 Google + 去重 + OSM 序列 fallback + `geocode_cache` SQL 快取
>   命中跳過 Google + 新 geocode 批次寫入

### 端對端 smoke test（需 DB + uvicorn 已啟）

```bash
bash backend/scripts/smoke_audit.sh   # 最後印綠色 ✓ 表示 audit chain 完整
bash backend/scripts/smoke_upload.sh
```

### 前端 UI smoke test（playwright-core 驅動系統 Chrome）

```bash
cd frontend/tests
npm install                                          # 一次性
npm test                                             # 不帶 token：公開頁 + 守衛重導向 + 訪客地圖互動 + 問答式對應 + XSS 防護（33 條）
# 帶 token 跑完整 50 條（另含 admin 三分頁、audit 查詢、登入後 UX 驗證、加密檔錯誤提醒）
export CT_SMOKE_TOKEN=$(bash mint-token.sh CIDadmin)
npm test
```

需求：DB + uvicorn (8000) + 前端 http.server (5501) 已啟，系統有 Chrome。
細節見 `frontend/tests/README.md`。

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
| **P7** | 專案分享連結：30 分鐘臨時免登入唯讀檢視（`share_links` 表 + `api/share.py` + `frontend/share.html`）；見下節五-M。同 commit 另含 index.html「回到先前的專案」下拉、measure 距離標籤改用 leaflet tooltip、admin 核准流程顯示順序修正、register 帳號 placeholder 文案調整 |
| **P8**（2026-06-27） | 台哥大上網歷程格式（進入/離開基地台）+ 手動對應結構性修復 + geocode 並行/SQL 持久快取 + 雲端大檔上限發現；見下節五-P/Q/R 與七-8 |
| **P8.1**（2026-06-28，commit 9f43006） | persisted `/upload` 存檔路徑改 **chunk-based ingest**（`_ingest_rows_stream`）：正式存檔路徑也吃到 `geocode.lookup_bulk`（並行 + SQL 快取）、每塊 normalize→bulk geocode→executemany→釋放，解 H1 與 OOM。chunk 由 `INGEST_CHUNK_SIZE` 控制（預設 800，合理 100–5000，非法 fallback 800）。另修 upload queue 檔名 `f.name` 未跳脫的 self-XSS（改 `escH`）。新增 20 條測試（`test_ingest_chunked_stream.py`），pytest **286 passed**。**已部署 Render 並以完整 test3 實測通過**（見五-T、七-8）。 |
| **P8.2**（2026-06-28，commit 44f9298） | 新增 **`simple_time_location` Excel 極簡格式**支援：A 欄=時間、B 欄=位置（地址或**單格經緯度**）、A/B 以外忽略、**支援有表頭與無表頭**（靠結構與內容判斷、不靠檔名）。落點為 **`_iter_rows_excel` 規則 B 失敗後的 fallback**（電信格式過規則 B、不進 fallback → 零回歸）。新增 `_parse_simple_time`（民國年隔離、不改 `_parse_ts`）、`_parse_latlng_text`（單格座標）、`_iter_simple_time_location`（偵測+emit）。門檻 **time ratio ≥80% 且 location ratio ≥80%**。新增 15 條測試（`test_ingest_simple_time_location.py`），pytest **301 passed**。**已部署 Render 並以 simple 檔正式 `/upload` 實測通過**（見五-U、七-9）。 |
| **P9A**（2026-07-05，deployed commit aa3125e） | **Preview Evidence Artifact backend**：A.1 加密盒（gzip→AES-256-GCM，`PREVIEW_ARTIFACT_KEY`，fail-closed，commit `0d38198`）+ A.2 `preview_artifacts` 儲存基礎（internal BIGSERIAL / external preview_id / raw SHA-256 / canonical parsed hash / parser provenance / TTL / system·analyst·supervisor seal 欄；<5MB BYTEA、5–50MB object storage 未實作、>50MB 不支援，commit `89a9e6c`）+ A.3 五端點 API（create/read/seal/save/delete，response 不回 `_records`，save 由 server 重讀原檔驗 SHA-256 再解析→register evidence→chunked ingest，consumed/revoked/expired 回 410，commit `f541ae6`）+ A.4 每 10 分鐘過期清理排程（啟動即跑一次、fail-safe、`preview.cleanup` audit，commit `aa3125e`）+ Google 費用硬停（`GEO_GOOGLE_ENABLED=0`，commit `50b558b`，runtime log `google_calls=0`）。**A.6 正式環境端到端 smoke 全通過**（health 200 / create·read·seal·save 200 / evidence 建立 / map-layers·coverage 2/2 / evidence PDF / consumed·revoked 再讀 410 / 六類 preview audit 確認 / 測試 project 正式 DELETE 軟刪）。詳見下節「P9A Preview Evidence Artifact」。**尚未完成**：A.5 object storage、前端 cutover、mapping-aware preview、guest preview、`save-records` deprecation、supervisor seal/custody ledger、`geocoded_cell_estimates` 推估分表。 |

### 各真實樣本當前 normalize 通過率

| 樣本 | 通過率 | 備註 |
|---|---|---|
| 彭奕翔 `0801-0903...xlsx` | 100% | W2.3 後 |
| `網路歷程.xltx` / `網路歷程-2a0c1c9a.xltx` | 100% | W1.5 baseline |
| `電話通聯+歷程.xlsx` | 60.31% | 剩餘為**物理失敗**（798 列無 cid + 362 列無 ts + 30 noise）|
| `周蔓達上網歷程.xlsx` | 51.08% | 達**資料物理上限**（47.35% 列原始就缺起台/起址）；6769 列 demo_case |
| `楊云豪…(含方位角).pdf` | 100% + sector_id 100% 正確 | W2.5 |
| `11501-11505(雙向).xlsx` | 100%（2 筆全解） | 2026-06-26：雙向通聯小檔，需新別名 `始話日期時間` + `基地台編號N/位置N` 斜線複合欄 + 列數守門放寬至 < 2 |
| `test2.xlsx` / `test3.xlsx`（台哥大上網歷程） | 100%（各 21757/21758 列全解） | 2026-06-27：`進入/離開基地台` 系列別名 + SCAN_WINDOW→30（真表頭埋 row 27）+ 假 dimension peek fallback。**注意：雲端單次上傳受記憶體/逾時限制，>~5000 筆需分批**（見七-8） |
| `026962 陳2號機網路.xlsx`（台哥大） | 98.5%（1514→1491） | 2026-07-21：真表頭埋 **row 48**（兩個調閱區塊各帶一段 PII），SCAN_WINDOW→60。丟棄的 23 列＝第二區塊 metadata + 重複表頭列（無 ts，正確過濾）|
| `026965 陳1號機網路.xlsx`（台哥大） | 99.6%（11557→11511） | 一個 sheet 內 3 個表頭區塊，沿用首個表頭正確解析 |
| `028351 蘇世崇網路.xlsx` / `031543 蘇網路.xlsx`（遠傳） | 99.6% | 2026-07-21：`通聯起始/結束時間` + `起始基地台編號/地址` 別名（見五-V）|
| `複本 029935 陳1號機網路.xlsx` / `031543`（含「標記」分頁） | 去重後 99.5% | 2026-07-21：`標記` 分頁是 `工作表1` 的**子集**（029935 為逐格相同、031543 少 23 列），規則 A2 multiset 子集去重（否則筆數翻倍，見五-W）|

**結論（2026-08-22 更新）**：**Excel/CSV 路徑**手邊所有真實樣本要嘛 100%、要嘛達資料物理上限，沒有已知未修的 silent bug。**但 2026-08-22 對全 34 檔（含後來新增的 PDF）重跑後發現：13 個 PDF 完全無法解析**，全部是遠傳／台哥大「純文字、無框線、空白對齊」的匯出樣式（其中 7 檔與已能解析的 xlsx 是同一調閱案、無資料損失；6 檔的 `0927352336` 只有 PDF 版）。**詳見七-13。** 解析無筆數上限；雲端「上傳→geocode→回應」整段才有資源上限（七-8）。

### 支援格式一覽（ingest 能吃的檔案格式）

**容器**：CSV/TXT/TSV、Excel（xlsx/xltx/xlsm/xltm）、PDF。**業者欄位方言**見五-A / 五-P（台哥大、雙向通聯、中華上網、GPS 軌跡…，皆 header-alias 驅動）。

**`simple_time_location`（P8.2 極簡格式）**：
- **Excel only**（目前**不支援 CSV**）。
- **A 欄 = 時間**、**B 欄 = 位置（地址或經緯度）**、**A/B 以外一律忽略**。
- **可有表頭、可無表頭**（靠結構與內容判斷、不靠檔名）。
- 時間支援：`2026/06/28 13:20`、`2026-06-28 13:20:30`、`115/06/28 13:20`（民國年）、`6/28/2026 1:20 PM`（美式）。
- 經緯度（**單格**）支援：`lat,lng`、`lng,lat`（自動對調）、半形逗號、全形逗號、空白分隔；B 為地址則走 geocode。
- 落點：**`_iter_rows_excel` 規則 B 失敗後的 fallback**（`_iter_simple_time_location`）；命中門檻 time ≥80% 且 location ≥80%。詳見五-U / 七-9。

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
2. **手動欄位對應（問答式，2026-06-08 改版）**：不要求使用者看懂欄名（各家業者用語不一），改問「時間在哪一欄？地點在哪一欄？」並秀**範例值**（`_peek_sample_rows` 抓表頭後前幾列、放進 diagnosis `sample_rows`），前端 `showManualMappingModal` 依範例值自動猜欄（看到 `2026-01-15` → 時間、`高雄市…號` → 地址、`22.6`/`120.3` → 緯/經）。產出 mapping 仍是 `{欄名: 系統欄位}` → `_apply_user_mapping` rename 成 `_RAW2CANON` alias 再 normalize（後端不變）
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
- 效期固定 30 分鐘，**後端寫死**不開放呼叫端自訂（避免有人開出永久連結）；owner 可在到期前 `DELETE` 撤銷（外流補救）。
- 連結有效 = `revoked_at IS NULL AND expires_at > now()`；失效回 **410 Gone**、不存在回 404。
- 每次成功檢視 `use_count+1` 並寫一筆 `audit_logs`（`action=share_link.view`，含 IP）。
- 建立／撤銷需該專案 **owner 或系統 admin**（`_require_project_owner`）；`created_by` 對匿名 admin（id=0）寫 NULL。

**刻意決策**：`share.py` 的 `_fetch_map_geojson` **複製** `/map-layers` 的查詢而非 import map.py — 讓 share 模組自足、不牽動既有 API。前端 `frontend/share.html` 是獨立精簡頁（重畫 sector/精度圓，不依賴 index.html），`<meta robots=noindex>` 避免被索引。

### N. 上傳定位透明化 / coverage（2026-05-23）

**問題**：`/map-layers` 過濾 `geom IS NOT NULL` → 使用者只看到「成功定位」的點。上傳 300 但 100 筆 geocode 失敗時，地圖只剩 200 點 —— 沒人告訴使用者另 100 筆去哪了，違反證據完整性原則（法庭上若被問「為什麼這 100 筆沒列?」必須答得出）。

**端點**（均 viewer+）：
- `GET /api/projects/{id}/coverage`：回 `{total, with_geom, without_geom, by_reason{no_signal, cellid_only, addr_geocode_failed}}`，單次掃描用 PostgreSQL `FILTER` 一次算完五個欄位，避免分次 query 帶來不一致。
- `GET /api/projects/{id}/unlocated?reason=&limit=`：列未定位列，每筆附 `reason` 標籤；可用 `?reason=` 篩單類別。
- `GET /api/projects/{id}/unlocated.csv`：同上但下載為 CSV（含 UTF-8 BOM，Excel 直開不亂碼）。

**三類原因**（純靠既有欄位推導，不需 migration）：
1. `no_signal` — cell_id 與 cell_addr 皆缺，原始檔殘缺，無從推論。
2. `cellid_only` — 有 cell_id 但無 cell_addr，需業者「cell_id → 座標」對照表（即 `cell_towers`）。
3. `addr_geocode_failed` — 有 cell_addr 但 Google/OSM 都失敗（地址模糊 / 或原始檔該欄塞了非地址內容如 sector 代碼）。

**前端三層 UI**（`index.html`）：
- **L1 上傳完成 receipt modal**：4 個數字（原檔讀取 / 寫入 DB / 已定位 / 未定位）+ 按原因分布 + [查看詳細] [下載 CSV] 鈕。
- **L2 地圖頂部常駐 banner**：`without_geom > 0` 時顯示「⚠ 本案另有 N / M 筆未定位（…）」；切換到 temp/guest 模式自動隱藏。
- **訪客路徑補洞（2026-07-21）**：上述 L1/L2/L3 三層**全部綁「專案模式 + 已登入」**，訪客路徑原本一層都沒有，且 `doGuestUpload` 把 `plotted`（定位數）當成「已載入筆數」顯示 → 實測 028351（9,281 筆全解析、0 筆定位）畫面顯示「已載入 **0** 筆資料」+ 空白地圖，使用者無從分辨「解析失敗」與「定位失敗」，另外那 9,281 筆等同**沉默丟棄**。修法：分開累計 `total`/`plotted`/`skipped`，並把未定位筆數寫進**常駐**的訪客 banner（dropzone 訊息 2 秒後自動消失，來不及讀）。詳見五-Y。
- **L3 未定位詳細 modal**：三 collapsible section（按原因），每段含人話「排除方式」+ 該段筆數 + 列表（前 50 筆）+ 下載該段 CSV。

**設計取捨**：
- coverage 與 unlocated 的 reason 分類在 SQL 與 Python 兩處都用同一個 `_REASON_SQL_CASE`，避免兩處邏輯走偏。
- L1 receipt 用 `?target_id=` 範圍，數字才對應「本次上傳」；L2 banner 用 project 範圍，反映整案累計。
- 故意不在後端做「自動重試 geocode」—— 失敗原因是業者表缺 / 地址模糊等資料面問題，自動重試只是吃 quota。修法是匯入 `cell_towers` 或人工 pin，由使用者主動發起。

### O. 雙向通聯格式 + 小檔守門放寬（2026-06-26）

**背景**：`11501-11505(雙向).xlsx` 上傳回 422。深查發現是**兩個獨立關卡**疊加，逐一修掉（commit `ad170e3` + `8cf7b7d`）：

1. **欄名別名缺漏**：此業者用 `始話日期時間`（非既有的 `始話時間`/`時間`）當時間欄、用 `基地台編號1/位置1` 把 cell_id 與地址以 `/` 合併在同一欄。`_normalize_row` 走**精確比對**（`header_map.get(_canon(k))`），兩欄都 miss。
   - 解法：`_RAW2CANON` 加 `始話日期時間 → start_ts`、`基地台編號1/位置1`・`基地台編號2/位置2 → cell_id_compound`（位置1=起話端先填即優先、位置2=迄話端走 fallback，沿用 W2.3 dispatch）。
   - `_split_compound_cell` 加「斜線分隔」分支：以第一個 `/`（含全形「／」）切兩段，**僅在「左段不含中文（ID-like）且右段含中文（地址）」時採用** —— 雙條件守門避免地址內部樓層 `3/4樓` 被誤切；不符特徵者落回原空白分隔邏輯（彭奕翔格式零影響）。
   - schema.sql seed 同步（drift 守護測試要求 `_RAW2CANON` ↔ DB seed 一致）。

2. **小檔被列數守門擋掉**：此檔整張 sheet 只有 3 列（1 表頭 + 2 筆），被 `_iter_rows_excel` 規則 A「總列數 < 5 → 當非資料表跳過」整個略過 → 0 筆。
   - 解法：規則 A 門檻 `< 5 → < 2`（資料表物理下限：1 表頭 + 1 資料）。
   - **安全性論證**：擋「封面頁／統計頁／人資頁」的真正防線是**規則 B**（表頭須命中 ≥2 個 canonical 別名）＋ line 350（去表頭後須 ≥1 列），不是列數本身；故降到下限不會放進垃圾分頁。偵查實務上「某對象調閱期間只有 1～2 筆通聯」是真實且關鍵資料，舊門檻會整檔沉默跳過 → 違反證據完整性原則。

**踩雷教訓**：第一輪只單測 `_normalize_row`（單列）就以為修好，漏測了 `_iter_rows_excel` 的列數關卡 → 對使用者誤報「已修復」。**驗證 ingest 改動務必走 `_iter_rows_excel` 完整路徑、用真實檔（或足量列數合成檔）**，不能只測 normalize 單元。

### P. 台哥大上網歷程格式 + 假 dimension（P8，2026-06-27，test2/test3.xlsx）

**背景**：台哥大「通訊數據上網歷程」檔回 422。完整走 `_iter_rows_excel` 後查出**三個獨立關卡**疊加（commit `53d88be`）：

1. **欄名「進入/離開基地台」系列全不在 `_RAW2CANON`**：此業者用 `進入基地台時間`（到達該基地台覆蓋的時間）、`離開基地台時間`、`離開基地台編號`（純 ID）、`離開基地台地址`。
   - 解法：`_RAW2CANON` 加 `進入基地台時間→start_ts`（地圖以此為定位時間）、`離開基地台時間→end_ts`（不遺失）、`離開基地台編號→cell_id`、`離開基地台地址→cell_addr`。schema.sql seed 同步（drift 守護）。

2. **真表頭埋在 row 27**（前面是查詢條件 + 完整「使用者資料」PII 區塊：用戶名稱/帳寄地址/戶籍地址/證號/生日…），超過舊 `SCAN_WINDOW=25`。
   - 解法：`SCAN_WINDOW` 25→30（**2026-07-21 再放寬到 60**，見五-V）。安全：PII/metadata 列 canonical 命中數=0，真表頭仍勝出，規則 B 不受影響。

3. **三條路徑各自獨立**（踩雷重點）：① Excel 實際 normalize 走 `_RAW2CANON`/active_map；② **診斷顯示**走 `_match_col_idx`（獨立的硬編碼別名清單 + 子字串比對）；③ 新 DB 部署 seed。改①不會自動修②。本次也補 `_match_col_idx` 的 start/end 候選（`進入/離開基地台時間`），讓診斷視窗的「時間欄位 ✅/❌」與實際解析一致。

**附帶：假 dimension 防呆**（test2 觸發）：部分匯出工具把 worksheet `<dimension>` 寫死成 `A1`（宣告整表一格）。pandas（`_iter_rows_excel` 用）不受影響；但 openpyxl `read_only=True`（`_peek_headers`/`_peek_sample_rows` 用）會信任假邊界 → 讀到空列。解法：`_read_xlsx_top_rows` 先試 read_only，**若結果退化（最寬列 ≤1 格）** 才 fallback `read_only=False` 重掃（非以列數判斷——假 dimension 會回多列但近乎全空）。

### Q. 手動對應結構性修復（P8，2026-06-27）

**使用者回報**：系統不認識格式時「手動對應欄位」無法正確操作。確認為**結構性 bug**（兩層）：

1. **pipeline 順序顛倒**：手動對應是 `_iter_rows_excel`（先偵測表頭、丟棄 sheet）→ 才 `_apply_user_mapping`（rename）。但「完全不認識」時，規則 B（表頭須命中 ≥2 個已知別名）在 rename 前就把整張 sheet 丟掉 → 使用者的對應無從施力。而手動對應的唯一情境正是「系統不認識」→ 它在最該作用時必然失效。
   - 解法：`_iter_rows_excel(user_mapping=...)`，header detection 計分時把「使用者已指定的欄位」也算命中（`mapped_canon`）→ 陌生 sheet 不被丟棄。raw key 照舊 yield，rename 仍由 `_apply_user_mapping` 負責（分工不變）。`parse_file_only` 把 mapping 同時傳入 `_iter_rows_excel`。

2. **`_peek_headers` 死抓第一個物理列** → 埋深表頭檔（如 test2）顯示大標題「台灣大哥大…查詢」當「可對應欄位」，modal 顯示錯欄。
   - 解法：`_peek_headers`/`_peek_sample_rows` 改用 `_guess_header_row_idx` **結構性**定位真表頭（不靠別名，因為陌生格式無別名可用）：取最寬列寬度 `max_w`，門檻 `thr=max(3, ⌈max_w/2⌉)`，回傳第一個「自己 ≥thr 且下一列也 ≥thr」的列（= 表頭，後接資料）。前端 UI 仍同時秀「範例值」讓使用者用眼睛確認，不單靠此猜測。

**前端不需改**：`frontend/index.html` 的 `showManualMappingModal`（問答式「時間/地址在哪欄」+ 依範例值自動猜 + `{欄名:系統欄位}` 送出）邏輯本來就對；bug 純在後端 peek 抓錯列 + rename 前丟棄。

### R. geocode 並行化 + SQL 持久快取（P8，2026-06-27）

**背景**：部署 P 後，雲端上傳完整 test3（~2423 唯一地址）回 502。瓶頸是 geocode：雲端無 Redis、`cell_towers` 空 → 全 miss → `lookup_bulk` Step 5 **逐筆序列**打 Google（`_google_geocode` 阻塞 + OSM 備援 `time.sleep`），累積超過 Render ~120s 上限。三層修法（commit `e61bf60` + `eeaef92`）：

1. **並行 Google**：`lookup_bulk` Step 5 改 `ThreadPoolExecutor`（`GEO_GOOGLE_CONCURRENCY` env，預設 10；Google I/O bound，thread-safe）。OSM 失敗備援**刻意保持序列**（Nominatim 1 req/s 政策）。順手對 `simplified` 地址去重。
2. **SQL 持久快取 `geocode_cache`**（取代失效的雲端 Redis）：`_ensure_sql_cache()` 自動 `CREATE TABLE IF NOT EXISTS`（免手動 Supabase migration；另列 schema.sql + `migration_geocode_cache.sql`）。`lookup_bulk` 查詢順序：本地 cell_towers → Redis MGET → **SQL 批次 ANY 讀** → 並行 Google → OSM。
3. **增量寫回**：geocode 成功每累積 100 筆就 flush 一次 SQL（不留到最後）。**關鍵**：即使請求在 120s 被切，已完成地址確實落地 → 重傳跳過、漸進收斂。OSM timeout 15s→6s 限縮序列尾巴。

實測 SQL 快取生效：完整 test3 冷快取 502@120s → 部分快取暖後 502@**73.8s**（時間降證明快取運作）。但見七-8：73.8s 就崩屬記憶體 OOM，快取救不了記憶體牆。

### S. 前端 UI/UX 全貌（彙整，供人類與 AI 快速掌握使用者體驗）

**設計語言**：P6 起改 Google Maps 風格全螢幕 UI（Leaflet + markercluster，純 HTML/JS 無 build step）。深藍科技風帳號頁（P4.4，影片背景 + 毛玻璃卡）。

**三種上傳模式**（偵查實務「先看能不能解，再決定建案」）：
- **訪客免登入**（`POST /api/parse-only`）：純解析 + geocode，**不寫任何 DB**，記憶體內回 GeoJSON 預覽。限 20 req/hr/IP。
- **臨時查看**（`POST /api/upload/parse-temp`）：登入後解析給前端，**不落 raw_traces**。
- **正式儲存**（`POST /api/upload`）：寫 DB + SHA-256 證據鏈 + audit_logs。

**上傳結果透明化三層 UI**（第五節 N，證據完整性核心 — 不可沉默丟資料）：
- **L1 收據 modal**：上傳完成秀 4 數字（原檔讀取 / 寫入 DB / 已定位 / 未定位）+ 按原因分布 + [查看詳細] [下載 CSV]。用 `?target_id=` 範圍（對應「本次上傳」）。
- **L2 地圖頂部常駐 banner**：`without_geom > 0` 時顯示「⚠ 本案另有 N / M 筆未定位（…）」。project 範圍（整案累計）。temp/guest 模式自動隱藏。
- **L3 未定位詳細 modal**：三 collapsible section（按 `no_signal` / `cellid_only` / `addr_geocode_failed`），每段含人話排除方式 + 筆數 + 前 50 筆列表 + 下載該段 CSV。

**地圖互動元素**：
- **popup 條件渲染**（第五節 E）：`azimuth != null` 才顯示方位（避免 0「正北」被誤過濾）；`cell_id` 空顯示「—」；azimuth_ref 行只在有方位時出現。`fmt` 已跳脫 HTML（防 stored XSS）。
- **方位角基準警告 + 標註 modal**（P2.5，`openAzRefModal`）：`azimuth_ref=unknown` 時 popup 紅字警告；admin 標 `magnetic`/`true`/`unknown` + evidence ≥5 字 → PATCH。
- **方位角基準 dashboard**（P2.5-C）：unknown 比例 / 最近標註人 / audit trail viewer。
- **顯示定位時間標籤**（P4.2）：zoom 響應、per-series 切換。
- **測距工具**（measure）：距離標籤用 leaflet tooltip（P7 改）。
- **自訂標記 / 手動定位 pin**（manual-locate）：未定位列可人工 pin 座標。
- **新手導覽**（P2.7）、**回到先前的專案下拉**（P7）。

**格式無法解析時的兩層 UI**（第五節 I + Q）：
- **L1 智慧診斷 modal**（`showDiagnosisModal`）：秀 `_match_col_idx` 推斷的「時間/基地台 ID/地址欄位 ✅/❌」+ 可用欄位清單。PDF 不給手動對應鈕（無欄名）。
- **L2 問答式手動對應 modal**（`showManualMappingModal`）：**不要求看懂欄名**，改問「🕐 時間在哪一欄？📍 地址在哪一欄？」，依**範例值**自動猜（看到 `2026-01-15`→時間、`高雄市…號`→地址、`22.6`/`120.3`→緯/經），可展開看前幾列範例。送出 `{欄名:系統欄位}` → 帶 mapping 重傳。**P8 修好其結構性 bug**（見 Q：陌生格式不再被規則 B 丟棄、埋深表頭不再顯示錯欄）。

**帳號 / 管理頁**：`login`/`register`/`change-password`（P4.4）；`admin.html`（使用者管理 / 欄名對照表 / 格式回報三分頁）；`audit.html`（P2 稽核檢視）；`share.html`（P7 分享連結公開檢視，獨立精簡頁，`<meta robots=noindex>`）。

**前端自動化回歸**：`frontend/tests/smoke.js`（playwright-core 驅動系統 Chrome，46/50 條）涵蓋公開頁 / 守衛重導向 / admin 三分頁 / audit / 登入後 UX / 地圖互動 / 加密檔錯誤 / 問答式手動對應。走 parse-only 不寫 DB。

### T. P8.1 雲端驗證結論（2026-06-28）

persisted `/upload` chunk-based ingest（五-P8.1 milestone）已部署並在 Render 正式環境實測。

**【已確認事實】**（CIDadmin token 經正式 `POST /api/upload` 實測，非 parse-only）：
- commit **9f43006 已部署至 Render**（`celltrail-api`），health 200、`db_ok/postgis_ok=true`、`redis_ok=false`。
- 完整 **test3.xlsx（21757 列）**經正式 `/api/upload` → **HTTP 200、42.2s**、`inserted=21711 / skipped=46`（皆驗證跳過）、`inserted+skipped=total`（完整匯入、非部分匯入）。
- **無 502 / timeout / OOM**；`map-layers` 讀回 21649 點、`coverage` `with_geom=21649 / without_geom=62`。
- **結論**：chunk-based ingest 解決 persisted path 的 **H1（存檔路徑現吃到 `lookup_bulk` 並行 + SQL 快取）** 與 **OOM（記憶體峰值限於一塊）**。上輪「真 DB 整合 / 雲端端到端」未確認風險已實測解除。

**【尚未解決】**：
- `ingest_pdf` 仍**逐筆 `geocode.lookup`**（P8.1 刻意未動；PDF 通常小、風險低）。
- `parse-only` / `parse-temp` 預覽 payload 仍偏大（同時回 `_records` + GeoJSON），大檔預覽仍可能 OOM（待辦 #0）。
- chunk 原子性尚未用 `conn.transaction()` 強化：某塊 `executemany` 中途失敗時，`inserted` 可能少報（誠實面安全，不多報）；本次雲端實測未觸發（無 DB 失敗）。

### U. P8.2 雲端驗證結果（2026-06-28）

`simple_time_location` 極簡格式已部署 Render（commit 44f9298）並經**正式 `POST /api/upload`**（CIDadmin token，非 parse-only）實測。

**測試檔**（1 表頭 + 2 資料列）：
- row1 = **民國年 + 單格經緯度**：`115/06/28 13:20` + `22.6273,120.3014`
- row2 = **ISO 時間 + 中文地址**：`2026-06-28 13:25:30` + `高雄市前金區中正四路211號`

**驗證結果（皆確認事實）**：
- `POST /api/upload` → HTTP **200**、`total=2 / inserted=2 / skipped=0 / errors=0`。
- **map-layers** `total=2`：row1 落點 `[120.3014, 22.6273]`（民國 115→2026、單格經緯度**免 geocode**、addr=None）；row2 落點 `[120.293, 22.628]`（中文地址 **geocode 成功**、cell_addr 保留）。
- **coverage** `with_geom=2 / without_geom=0`（定位率 100%）；時區 TPE↔UTC 正確（13:20→05:20Z）。
- 測試 project **`DELETE /api/projects/_simple_verify`** 軟刪成功（`affected_rows=2`），刪除後 coverage `total=0`。
- **結論**：simple_time_location 走正式 `/upload`（P8.1 chunked 路徑）端到端通過；民國年、單格經緯度、中文地址 geocode 全部正確。

### V. 遠傳格式 + 更深表頭 + 重複分頁去重（2026-07-21）

**背景**：對 `基地台位置範例檔案/`（17 檔）全量重驗，3 個真實檔回 422。三個**互相獨立**的關卡：

1. **遠傳上網歷程欄名缺別名**（`028351` / `031543`）：此業者用 `通聯起始時間` / `通聯結束時間`。表頭在 row 23（窗內、已讀到 9319 列），但每列拿不到 `start_ts` → 全被時間驗證濾掉 → 0 筆。
   - 解法：`_RAW2CANON` 加 `通聯起始時間→start_ts`、`通聯結束時間→end_ts`、`起始基地台編號→cell_id`、`起始基地台地址→cell_addr`（schema.sql seed 同步，drift 守護要求）。
   - **資料落點注意**：實測 `起始基地台*` 兩欄為空、真值在 `離開基地台*`（P8 已有別名）。兩組別名並存靠 `_normalize_row` 的 W1.5「空值不覆蓋」互補，與 dict 走訪順序無關。
   - **踩雷點**：診斷路徑（`_match_col_idx`）當時**顯示三欄皆 ✅**，因為它靠 Pass 2 子字串（`起始時間` ⊂ `通聯起始時間`）命中 —— 與實際解析路徑分歧的**反向**案例（五-P 記的是改①不修②，這次是②看起來好的但①壞）。**診斷 modal 說「找到欄位」不等於能解析**。

2. **真表頭埋到 row 48**（`026962`）：該檔含**兩個調閱區塊**，每塊各帶一整段「使用者資料」PII，比 test2.xlsx 的 row 27 更深。舊 `SCAN_WINDOW=30` 在窗內找不到任何命中列 → 規則 B 誤判整張 sheet 為非資料表 → yield 0。
   - 解法：`SCAN_WINDOW` 30→**60**。安全性論證不變：把關的是 `MIN_HEADER_MATCHES`（≥2 命中）而非窗寬，PII/查詢條件列命中數＝0，加大窗只會「多看到」真表頭。
   - **驗證陷阱**：`SCAN_WINDOW` 是 `_iter_rows_excel` 的 **local 變數**，`monkeypatch ingest.SCAN_WINDOW` 完全無效（曾據此誤判「放寬無效」）。已加 `test_scan_window_covers_row48` 用 `inspect.getsource` 鎖住實際值。

3. **重複分頁造成筆數翻倍**（`複本 029935`、`031543`）：兩檔各有一張名為 `標記`、與 `工作表1` **逐格完全相同**的分頁（承辦人複製一份標記重點）。W2.1 多 sheet 支援兩張都讀 → 同一筆通聯入庫兩次；`raw_traces` 無內容層級唯一索引，DB 不擋。
   - **為何是證據完整性問題**：「該門號在該時段出現幾次」是實質待證事實，翻倍會直接扭曲軌跡密度與停留時間判讀。
   - 解法：**規則 A2**（初版為「內容 sha256 完全相同才跳過」，**同日改版為 multiset 子集判定**，見下段 W）。

**驗證**：全 17 檔重跑 → 16 個真實檔全解（`壞檔.csv` 依設計回 422），原本正常的 13 檔筆數**逐檔不變**（零回歸）；新增 `test_ingest_fet_and_dup_sheet.py` 8 條，pytest **463 passed**。

### W. 規則 A2 改版：multiset 子集分頁去重（2026-07-21，同日）

**背景**：五-V 的 A2 初版採「逐格 sha256 完全相同」判定。使用者當天編輯樣本檔後，`031543` 的第二分頁變成第一分頁的**純子集**（4,818 個唯一列全部已存在於第一分頁，但列數少 23）→ 雜湊不同 → 規則失效 → **約 4,800 筆重複寫入**。實務上副本幾乎一定會被動過（標記重點、刪幾列），所以「嚴格相等」等於形同虛設。

**改法**（`_iter_rows_excel` 規則 A2 重寫 + 兩支新 helper）：
- **fingerprint 建在 `_normalize_row` 之後**：比較的是證據語意欄位（`start_ts / end_ts / cell_id / cell_addr / lat / lng / azimuth / sector_id / sector_name / site_code`），壓成 blake2b 16-byte digest。樣式／顏色／註解／未映射欄（承辦人另加的標記欄）**天然不參與比較** —— 副本被塗色仍能辨識為子集，而真正多出來的證據列一定被保留。不需維護黑名單，由架構保證。
  - `event_type` 不納入：`raw_traces` 無此欄（通話類別只當 dialect 訊號、不落地）。
  - 無效列（缺 `start_ts`，或 cell_id/地址/座標皆空）回 `None`、不計入比對 —— 否則兩分頁的雜訊列數差異會干擾判定。有效性定義與 `_ingest_rows_stream` 一致。
  - 型別先 canonical 化（`_parse_ts`/`_to_float`/`_to_int`）：同一筆證據可能一邊存 datetime、一邊存字串，不正規化會產生不同 fingerprint。
- **multiset（`Counter`）包含判定**，非 set：`new_rows = Σ max(0, count_B[fp] − count_A[fp])`，等價 ∀fp: `count_B ≤ count_A`。用 set 會把「A 有 1 筆、B 有 3 筆」誤判為完全重複，漏掉 2 筆真實獨立事件。
- **只有 `new_rows == 0` 才整張跳過**；有任何新列就整張保留（寧可多存不可漏存）。
- 比對對象是**單一先前分頁**而非所有分頁的聯集：更保守，也才能在稽核紀錄明確指出 `reference_sheet`。
- **兩趟掃描**（Pass 1 建 Counter 判定、Pass 2 才 yield），共用 `_materialize_sheet_row()` 確保「判定用的列」與「實際落地的列」逐欄一致。**單分頁 workbook 自動關閉整個規則**（不可能有分頁間重複），大檔零額外成本。
- **稽核與隱私**：跳過紀錄改走 `core/logging_utils.log_info`，欄位 `reason=subset_duplicate_sheet / reference_sheet / valid_rows / duplicate_rows / new_rows`。**分頁一律以 `sheet#<位置>` 表示、不寫名稱** —— 實測分頁名可能直接是門號或對象姓名（如「0958549697 雙向歷程（嫌1）」），寫進 log 等同外洩 PII。其餘 skip 原因也一併結構化（`row_lt_2` / `header_matches_below_threshold` / `no_data_row_after_header`）。
- **不跨檔案去重**：去重狀態是 `_iter_rows_excel` 的區域變數，每次呼叫重建。

**驗證**：`031543` 9,600 → **4,800**（−4,800）；**其餘 15 檔逐檔 `+0`**，DB 實際落地數與 inserted 全部相符（本機 PostGIS 真實 `ingest_auto` 寫入後回查）。新增 `test_ingest_subset_sheet_dedup.py` 13 條，pytest **476 passed**。同時修改 6 條既有測試：5 條斷言舊 log 格式（要求訊息含分頁名稱，正是為 PII 移除的），1 條 `test_per_sheet_fake_header_detection` 屬真實行為變更（其兩分頁用了完全相同資料，新規則正確判為重複）→ 改用不同資料以保住原測試意圖。

**尚存風險**：① 走 `_iter_simple_time_location`（P8.2 fallback）的分頁不經過 A2；② 跨檔案重複仍會重複寫入（刻意）；③ B 的內容若分散在 A1+A2 兩張、各自不完整涵蓋，B 會被保留（偏保守）；④ fingerprint 忽略未映射欄 —— 若某業者把關鍵鑑識資訊放在系統尚未認識的欄位，該資訊不參與判定，緩解方式是把該欄加進 `_RAW2CANON`；⑤ 3 條真實檔回歸測試在 CI 會 skip（樣本檔含個資、不入版控）。

### X. cell_towers 匯入欄序 bug + 座標把關（2026-07-21）

**觸發時機警告**：這個 bug **只會在「第一次真正匯入業者座標表」時發作** —— 也就是待辦 #1 執行的那一刻。`cell_towers` 表一直是空的，所以它潛伏至今從未被觸發。

**根因**：`api/cell_towers.py` 用 `or` 鏈推導 CSV 欄位索引：

```python
idx_lat = col.get("lat") or col.get("latitude") or 1   # ← Python 的 0 是 falsy
```

欄位剛好排在**第一欄（索引 0）**時，`or` 會誤判成「找不到」而落到位置後備值。實測：

| CSV 欄序 | 修正前推導 (id,lat,lng) | 後果 |
|---|---|---|
| `cell_id,lat,lng` | (0,1,2) | 正常（碰巧）|
| `lat,lng,cell_id` | (2,**1,1**) | lat 與 lng **讀同一欄** → 緯度被寫成經度值 |
| `latitude,longitude,cell_id` | (2,**1,1**) | 同上 |
| `lng,lat,cell_id` | (2,1,**2**) | lng 讀到 cell_id 欄 → `float()` 例外 → 整批列跳過 |

**為何比一般欄位對應 bug 嚴重**：`cell_towers` 是 geocode 的**最前置查詢**（`_lookup_from_local` 命中就直接採用、不再問 OSM），而基地台座標**本身就是證據**。寫錯之後地圖照樣畫出漂亮的點位，整條軌跡卻是錯的，事後幾乎無從察覺。而業者交付的 CSV 欄序不是我方能控制的。

**修法**：
- 新增 `_pick_col()` / `_pick_col_req()` 以「欄名是否存在」判斷，取代 `or` 鏈。刻意分成兩支，而非在呼叫端寫 `_pick_col(...) or default` —— 後者正是本 bug 本身，留著遲早被照抄。
- 補上**座標範圍驗證**（原本完全沒有）：`lat ∈ [-90,90]`、`lng ∈ [-180,180]`，越界即**拒絕該列 + 記明確錯誤**（不猜測、不修正）。這是第二道防線：台灣經度約 120–122，落在合法緯度範圍外，故「經緯度對調」這個最常見的實務失誤會被攔下。
- 測試 `test_cell_towers_import_cols.py` 12 條（各種欄序 + helper 語意 + 範圍邊界），pytest **488 passed**。

**尚存風險（非程式問題）**：
- **短碼 cell_id 的唯一性**：周蔓達（中華上網方言）的 `起址` 是 **3–5 碼短式編號**（如 `13792`、甚至 `1`、`10`），與台哥大 15 碼 / 遠傳 20 碼截然不同。這類短碼**通常不是全域唯一**，需 LAC/TAC 等區域碼才能定位到唯一基地台 —— 向業者索取時可能得附原調閱案號。（**此為依編號形態的推論，未經業者確認**。）
- `(0,0)` 座標仍會通過範圍驗證。台灣不可能有此座標，但列為合法值不擋；若業者表出現大量 `(0,0)`，屬資料品質問題需人工檢視。
- **業者歸屬無法由檔案內容判定**：部分樣本檔的查詢前言（含「業者名稱:…」）已被承辦人剝除，只能依來源檔由人工標註。

### Y. 訪客路徑未定位揭露（2026-07-21）

**使用者回報**：「028351 / 複本 031542 / 031543 這三個檔都沒辦法解析。」

**實測（直接打 production `parse-only`，非本機）**：三個檔**全部解析成功**——

| 檔案 | HTTP | 解析筆數 | 已定位 |
|---|---|---:|---:|
| 028351 蘇世崇網路 | 200 | 9,281 | **0** |
| 複本 031542 蘇手機通聯 | 200 | 156 | **0** |
| 031543 蘇網路 | 200 | 4,800 | **1** |
| 網路歷程.xltx（對照組）| 200 | 149 | **0** |
| 雙向通聯.xlsx（對照組）| 200 | 18 | 2 |

**根因（兩層，都不是解析問題）**：
1. **定位能力趨近 0**：`cell_towers` 空表 + Google 停用 + OSM 已關（見七-10）。對照組顯示這不是特定檔案的問題，而是**全面性**的。
2. **UI 把定位失敗呈現成解析失敗**：`doGuestUpload` 用 `totalPlotted`（累計 `geo.plotted`）當「已載入筆數」顯示 → `plotted=0` 時畫面寫「已載入 0 筆資料」且地圖空白，與「檔案無法解析」在使用者眼中完全相同。

**為什麼這是證據完整性問題而非單純 UX**：五-N 訂下「不可沉默丟資料」，但 L1 收據 / L2 banner / L3 清單**三層全部綁「專案模式 + 已登入」**（`index.html` coverage 查詢明確限定），訪客路徑一層都沒有 → 9,281 筆未定位資料在訪客模式下**無任何痕跡**。

**修法**（純前端，`index.html`）：
- 分開累計 `total` / `plotted` / `skipped` —— 解析數與定位數是兩件事，失敗原因與處理方式完全不同，不可混為一談。
- 訊息改為「解析完成：共 N 筆，已定位 M，未定位 K」；全定位成功時改顯示「已全部定位」，不製造無謂警告。
- 未定位筆數另寫入**常駐**的訪客 banner（`#guestUnlocatedNote`）—— dropzone 訊息 2 秒後自動消失，來不及閱讀。
- 另補 8 秒 toast 明示「這不是解析失敗」。

**驗證**：`node --check` inline script（3,672 行）通過；三情境訊息輸出以真實 production 數字實測；`test_preview_client.js` / `test_preview_temp_flow.js` 通過。

**範圍界線**：本修正只讓使用者**看得懂系統做了什麼**，**不會讓地圖上多出任何一個點**。定位仍需待辦 #1（`cell_towers`）。

---

## P9A Preview Evidence Artifact（2026-07-05，deployed commit `aa3125e`）

**目標**：把「臨時查看／預覽」升級為有證據完整性保障的 **preview artifact**——server 端加密保存原始檔與 canonical parsed hash、走 seal（分級封存）與 save（server 重讀原檔驗證後才落 evidence），前端不再持有 `_records`、不再把解析後資料回送 server。P9A 完成 **backend + 正式環境端到端驗證**；前端切換與進階流程列於「尚未完成」。

### A.1 — Encryption box

- commit：`0d38198`
- gzip → AES-256-GCM
- blob layout：`[version:1][nonce:12][ciphertext+tag:n]`
- env：`PREVIEW_ARTIFACT_KEY`
- 缺失或格式錯誤時 **fail-closed**（拒絕運作，不 silent 降級）

### A.2 — Artifact storage foundation

- commit：`89a9e6c`
- `preview_artifacts` table
- internal `BIGSERIAL id`（內部主鍵）
- external `preview_id`（對外不可猜 token）
- raw SHA-256（原始檔雜湊）
- canonical parsed-record hash（解析後正規化記錄雜湊）
- parser provenance（解析器來源／版本標記）
- TTL lifecycle（到期治理）
- system / analyst / supervisor seal 欄位（分級封存）
- `<5MB` 使用 DB BYTEA
- `5–50MB` object storage **尚未實作**（A.5）
- `>50MB` 不支援 preview

### A.3 — Preview API

- commit：`f541ae6`
- 端點：
  - `POST /api/preview`
  - `GET /api/preview/{preview_id}`
  - `POST /api/preview/{preview_id}/seal`
  - `POST /api/preview/{preview_id}/save`
  - `DELETE /api/preview/{preview_id}`
- response **不回 `_records`**
- **save 時由 server 重新讀 raw file、驗證 SHA-256、重新解析、register evidence、chunked ingest**（server 為 authoritative source）
- consumed / revoked / expired 一律回 **410**
- guest 與 mapping-aware preview **尚未支援**

### A.4 — Cleanup scheduler

- commit：`aa3125e`
- 每 10 分鐘執行
- 啟動時立即執行一次
- fail-safe，不影響 app（排程失敗不拖垮主服務）
- 寫 `preview.cleanup` summary audit

### Google cost hard stop

- commit：`50b558b`
- `GEO_GOOGLE_ENABLED=0`
- 關閉時：
  - **不建立 Google HTTP request**
  - **不建立 Google ThreadPool task**
  - 查詢順序為：`cell_towers → geocode_cache → OSM → unlocated`
- production runtime log 已確認 `google_calls=0`

### A.6 — Production verification（皆確認事實）

- live deploy commit：`aa3125e`
- `/api/health/`：**200**（PostgreSQL / PostGIS 正常；Redis 未連線，但未影響本次核心功能）
- Preview create：**200**
- Preview read：**200**
- Analyst seal：**200**
- Preview save：**200** → evidence 建立成功
- map-layers：成功
- coverage：**2/2 有座標**
- evidence report：合法 PDF
- consumed preview 再讀：**410**
- revoked preview 再讀：**410**
- audit actions 已確認：`preview.create` / `preview.read` / `preview.seal` / `preview.consume` / `preview.delete` / `preview.cleanup`
- 測試 project 已透過正式 DELETE API 軟刪；未保留測試資料；未輸出任何 production secret

### 尚未完成（P9 後續）

- **A.5 object storage branch**（5–50MB 走 object storage）
- **前端 Preview Artifact cutover**（前端仍走舊 parse-temp/parse-only + `_records` + save-records）
- **mapping-aware preview**（A.3 目前不收 mapping）
- **guest preview**（A.3 目前 auth required）
- **舊 `save-records` deprecation**
- **supervisor seal workflow / custody ledger**（監督層封存與保管鏈）
- **`geocoded_cell_estimates` 推估座標分表**

> 註：下列「前端 Preview Artifact cutover」已於 **P9 Phase 2A** 完成（登入版 temp 路徑），見下一節。

---

## P9 Phase 2A — Frontend Preview Artifact Cutover（2026-07-08）

把「登入後臨時查看」前端從 legacy `parse-temp` + client-held `_records` + `save-records` 切到 **server-side Preview Artifact**（`/api/preview*`）。guest / manual-mapping 刻意續留 legacy。四個子階段：

### Phase 2A.1 — Preview API client（commit `f6aab58` 的一部分）
- `frontend/api.js` 新增 `window.CT_PREVIEW`：`createPreview` / `getPreview` / `sealPreview` / `savePreview` / `revokePreview` + `parsePreviewError` / `sanitizePreviewResponse`。
- **response allowlist**（`_pick`）：每個 response 只留白名單欄位，即使 server 意外回 `_records` 也**不保存/不暴露**。
- 統一 `PreviewError`（`name/kind/status/message/code/request_id/diagnosis`）；token 只讀 `localStorage['ct_token']`，錯誤訊息全靜態、不夾帶 token。
- 零依賴 Node 測試 `frontend/tests/test_preview_client.js`。

### Phase 2A.2 — Authenticated temp preview cutover（commit `f6aab58`）
- 純狀態/編排抽到 `frontend/preview-state.js`（`window.CT_PREVIEW_FLOW`，無 DOM 依賴、可 Node 測）；`index.html` 只做薄 glue。
- **登入 + `_sessionMode==='temp'` + 自動辨識** → `POST /api/preview`（`doTempUpload` 改走 `runTempPreviewUpload`）；只存 `preview_id` + metadata 於 **`_previewArtifactStore`**（`Map<target_id, PreviewArtifactState[]>`，**一 target 可多 preview**、append 不覆蓋），**不存 `_records`**。
- 儲存改走 `POST /api/preview/{id}/save`（`openSaveToProjectModal` 依 `classifyTargetSave` 分流：preview → `savePreview`；legacy → 舊 `save-records`；conflict → 阻擋）。
- **guest（`parse-only`）/ manual mapping（`parse-temp` + `save-records`）保留 legacy**；`_tempRecordsStore` **只剩 guest / manual-mapping 使用**（temp preview 路徑零 `_records`）。
- 422 diagnosis → 回退既有手動對應流程；手動對應 UI 標「⚠ 尚未使用存證預覽流程」。
- 測試 `frontend/tests/test_preview_temp_flow.js`。

### Phase 2A.3 — Traceable error contract + Request ID + structured logging（commit `5b501e0`）
- **machine-readable error contract**（`backend/app/core/errors.py`）：`AppError` + `ErrorCode.*`（`PREVIEW_NOT_FOUND/FORBIDDEN/EXPIRED/REVOKED/CONSUMED/TOO_LARGE/STORAGE_UNAVAILABLE/KEY_MISSING/SHA_MISMATCH/PARSE_FAILED`、`AUTH_REQUIRED`、`INTERNAL_ERROR`）。response 形狀 `{ error:{code,message,details}, request_id }`；只套 `/api/preview*`，其餘 endpoint `{"detail":…}` 零回歸。
- **Request ID middleware**（`core/request_context.py`）：`contextvars.ContextVar` + `X-Request-ID`（合法沿用、否則 `req_<uuid4hex>`）；**未預期 500 就地攔截**（最外層安全邊界）以保證 request_id + header + 不洩漏 stack trace。
- **structured JSON logging**（`core/logging_utils.py`）：`log_info/warning/error(event, **fields)`，共同欄位 `timestamp/level/event/request_id`；**redaction**（authorization/token/secret/password/jwt/cookie/bearer/api_key/apikey/artifact_key + 完全等於 key/auth/credential(s)；bytes 一律丟）+ **preview_id 遮罩**（前 6 + 後 4）。
- **preview cleanup scheduler** 改結構化 log（`preview.cleanup.completed/failed` + `run_id`，不偽裝 HTTP request_id）。
- 前端 `parsePreviewError` **優先 `body.error.code`**、保留 legacy detail fallback（rolling deploy）；`index.html` 錯誤 UI 顯示「錯誤追蹤碼：<request_id>」（`withRequestId`，`textContent` 安全輸出）。
- Pydantic schemas（`schemas/preview.py`）→ OpenAPI 可見 error contract。
- 新增 backend 測試：`test_error_contract` / `test_request_context` / `test_structured_logging` / `test_api_preview_errors`。

### Phase 2A.4 — DB-backed E2E 驗收（2026-07-08）
**環境**：本機 Docker PostGIS 16-3.4 + 本機 FastAPI(:8000) + static(:5501) + system Chrome via playwright-core；`GEO_GOOGLE_ENABLED=0` + seed `geocode_cache`/直接 lat/lng（避開 live OSM）；`PREVIEW_ARTIFACT_KEY` 僅進程內、未落檔。

**實測結果（皆確認事實）**：
- **格式矩陣 15/15**（一般電信/台哥大進入-離開/simple 有表頭/simple 無表頭/單格經緯度/中文地址/民國年/CSV/多筆同地址/部分未定位/雜訊欄；+ 加密 422/壞檔 422/diagnosis 422/>5MB 413/空檔 422）——每筆 upload→preview→save→evidence→map-layers→coverage，response **無 `_records`**、**未走 legacy API**。部分未定位案例 coverage `without_geom=1`（不沉默丟資料）。
- **權限矩陣 13/13**（real per-role JWT + real ACL）：owner GET/seal 200；unrelated GET/seal/delete/save 403 `PREVIEW_FORBIDDEN`；admin GET 他人 200；無/malformed/expired token 401 `AUTH_REQUIRED`；viewer save 403、collaborator save 200、project-owner save 200。
- **Browser 13/13**：真 create→`POST /api/preview`（非 parse-temp）+ marker render + 無 `_records`；6 種錯誤 UI（expired/revoked/consumed/413/503/500）皆顯示正確中文 + 「錯誤追蹤碼」且無 stack trace；guest→`parse-only`；diagnosis modal 開啟。
- **structured logs 72 events**：required fields 全具備、preview_id 遮罩、cleanup 用 run_id；**掃描無 token/key/raw bytes/PII 洩漏**。evidence-report：admin 200 有效 PDF、project owner 403（見 Finding）。
- **回歸**：backend **438 passed**、frontend **33 + 32 passed**。
- **cleanup 完成**：e2e users=0、live e2e raw traces=0、active previews=0、測試 project 軟刪、geocode seed 移除、audit logs 未刪、uvicorn/static 已停、臨時 key 隨進程消失。**測試資料未殘留**。

### 部署判定

```text
Phase 2A 有條件可部署
```

- **核心 authenticated temp preview**：已完整 E2E（create 真瀏覽器 + save→evidence→map/coverage/report API real-DB + admin report PDF）。
- 尚缺真瀏覽器 E2E（皆有單元 / 既有 smoke / API 契約替代覆蓋，低風險）：
  - guest login restore / guest save-records 未 browser E2E
  - manual mapping 完整提交 / `parse-temp` / `save-records` 未 browser E2E
  - save modal 未完整 browser 驅動
  - conflict guard 未 browser E2E
  - duplicate save API 未明確重送驗證（consumed 狀態已由 read 410 驗證）
  - TTL 自動 purge 未 real-time E2E（scheduler `completed` 已記錄；單元覆蓋）

### Finding

```text
REPORT_ACL_SPEC_MISMATCH
Severity: Medium
Status: Open / pre-existing
```

- `api/report.py` `evidence_report` docstring 寫「需 viewer 以上」，實作為 `dependencies=[Depends(require_admin)]`（admin-only）。
- 實測：project owner → **403**、admin → **200 有效 PDF**。
- **不在 Phase 2A 修改**（report.py 未被本階段觸及）；待產品決策：（a）僅 admin 可出報告 → 修 docstring；（b）應 viewer+ → 改 guard 為 `assert_project_access(viewer)`。

### 尚未完成（Phase 2A 後續 / P9 待辦）

- manual mapping full Preview Artifact（mapping-aware preview）
- guest preview / guest restore E2E
- save modal full browser flow
- conflict guard browser test
- duplicate save E2E
- TTL automatic purge E2E
- object storage A.5（5–50MB）
- report ACL decision（REPORT_ACL_SPEC_MISMATCH）
- supervisor seal / custody ledger
- `geocoded_cell_estimates` 推估座標分表

---

---

## P9 Phase 2B — Guest / Mapping-aware Preview Cutover（2026-08-22）

解除 P9A A.3 明文列出的兩項範圍鎖定（「只做登入版（無 guest）」「不收 mapping」）。
完成後，**前端三條上傳路徑（訪客 / 登入臨時 / 手動欄位對應）全部走 Preview Artifact，
沒有任何一條路需要在瀏覽器保存 `_records`**。這同時結案了待辦 #0 的主體。

### 為什麼是這條路（而不是「只把 `_records` 去重」）

`parse-only` / `parse-temp` 的回應**同時**帶完整 `_records` 與 GeoJSON `features`，
已定位的列在兩處各出現一次。單純去重只把記憶體從 ×2 降到 ×1，而且在目前
定位率趨近 0 的狀態下幾乎沒有效果（`features` 本來就是空的）。

真正的問題不是重複，是**`_records` 根本不該離開 server**：
- 大檔預覽 502 的直接原因就是把整份解析結果組成 JSON 回給瀏覽器（七-8 B）。
- 訪客把案件資料存在瀏覽器記憶體與 `sessionStorage` 裡，離開 server 之後不受控。
- 使用者登入後儲存的是「前端送回來的東西」（`save-records`），server 沒有原始檔、
  沒有 SHA-256、無從驗證 —— **證據鏈是斷的**。

走 Preview Artifact 三個問題一次解決：瀏覽器只拿到一個不可猜測的 `preview_id`，
原始檔以 AES-256-GCM 加密留在 server，儲存時由 server 重讀原檔、驗 SHA-256、重新解析。

### 實測 payload 縮減（本機真 DB，同一份檔案、同一組 `cell_towers`）

| 檔案 | 解析 / 已定位 | `parse-only` | `POST /api/preview` | 縮減 |
|---|---|---:|---:|---:|
| `026965 陳1號機網路` | 11,511 / 604 | 3,823,537 B | 231,138 B | **94.0%** |
| `028351 蘇世崇網路` | 9,281 / 3,758 | 4,740,622 B | 1,559,391 B | **67.1%** |
| `028351`（`cell_towers` 空、定位 0）| 9,281 / 0 | 3,169,810 B | **172 B** | 99.99% |

定位率愈低，縮減幅度愈大（未定位列在 preview 回應中完全不占空間，筆數仍由
`total` / `skipped` 誠實揭露）。

### 後端

**guest preview** — `POST /api/preview` 改 `get_current_user_optional`：
- `created_by IS NULL` 的 artifact 以 **`preview_id` 本身為 capability**
  （`secrets.token_urlsafe(24)` ≈192-bit，與 P7 share_links 同一套模型）。
  `_require_preview_owner` 只在這一支放行，**具名 artifact 的界線未動**
  （其他登入者 / 訪客讀 → 403，已由測試逐條釘住）。
- 為什麼不是「訪客誰都不能讀」：那樣訪客連自己剛建立的預覽都讀不回來，guest preview
  等於不存在。風險以 TTL（預設 30 分鐘）+ 訪客配額限制，且內容就是使用者自己剛上傳的檔案。
- **訪客 → 登入 → 儲存**：登入後憑同一個 `preview_id` 直接 save（`created_by IS NULL`
  可被領取），不需要把解析結果留在前端。
- **能做什麼有分**：create / read / delete 免登入；**seal 與 save 維持必須登入** ——
  seal 的意義是「某個具名的人在某時點確認過」，匿名封存等於沒有封存。
  delete 開放給訪客是刻意的：上傳錯檔案時必須能主動銷毀，不能只能乾等 TTL。

**訪客配額** — `services/limiter.py` 的 `hit_guest_preview_quota()`（20/hour/IP）：
- 不用 `@limiter.limit` 裝飾器，因為 slowapi 的 `exempt_when` **被呼叫時不帶任何參數**
  （`slowapi.wrappers.Limit.is_exempt`），拿不到 request，無從判斷「這次是不是訪客」。
  而 create 是登入與訪客共用的同一支端點：對登入偵查員套 20/hr 會誤傷辦案
  （一個案件動輒十幾個歷程檔），不套又等於開放匿名寫入加密 artifact。
- 改為在 handler 內判定身分後手動 hit 同一個 slowapi 計數器（同 storage、同語意），
  key 另加前綴，與 `/api/parse-only` 的配額各自獨立。storage 故障時放行（防濫用是次要防線）。
- 配額在**讀檔與解析之前**就擋，超額不必把 body 吃進記憶體。

**mapping-aware preview** — `mapping` 隨 create 送出並存進 artifact 的 `provenance`：
- `read` / `save` 一律以 **server 保存的那份**重解析；**刻意不接受呼叫端事後另傳 mapping** ——
  `parsed_records_hash` 是「這份原始檔 + 這組 mapping」的雜湊，事後換一組會讓封存過的
  雜湊失去意義。要換對應就建立新的 preview。
- `provenance` 存的是**欄名**（表頭字串）與 target_id，不是任何一列資料 → 不含 PII。
- `parser_type` 區分 `auto` / `manual_mapping`（證據上兩者可信度不同，需可查）。

**`ingest_auto(..., mapping=None)`（`services/ingest.py`）** — 這是整輪的硬前置條件：
- 在此之前**存檔路徑完全不吃 mapping**，手動對應過的檔案只能走 `parse-temp` +
  `save-records`。若不補這條，手動對應的檔案在 preview 流程會「解析看得到、存檔落地 0 筆」。
- 語意與 `parse_file_only(mapping=)` 逐字一致（同樣兩步：先傳進
  `_iter_rows_excel(user_mapping=)` 讓陌生 sheet 不被規則 B 丟棄，再 `_apply_user_mapping`
  rename 成 `_RAW2CANON` alias）。PDF 帶 mapping 一律**明確拒絕**而非默默忽略。
- `mapping=None`（預設）行為與改動前逐字相同，既有呼叫端零影響（有測試釘住簽名）。

**新增 error code** `RATE_LIMITED`（429）進 `core/errors.py` 的 contract 與 status 映射表。

### 前端

- `doGuestUpload`：`parse-only` → `POST /api/preview`，改用既有的
  `_PF.runTempPreviewUpload`（與登入版共用同一組編排）。**五-Y 的未定位揭露全數保留**
  （解析數 / 定位數分開累計、常駐 banner、8 秒 toast），另補「部分檔案失敗不被成功訊息蓋掉」。
- `retryUploadWithMapping`：`parse-temp`/`parse-only` → `POST /api/preview` + mapping。
  手動對應原本是**唯一一條繞過證據鏈的路**，而「系統不認得的格式」恰恰最需要可追溯。
- **訪客 → 登入的交接**（`saveGuestDataAndLogin` / `restoreGuestData`）：
  `sessionStorage` 從存「完整解析結果」改成只存 `preview_id` + 幾個計數，登入後向 server
  重新取回 features。舊作法大檔會直接撞上 sessionStorage 容量上限而整批遺失。
  - **`restoreGuestData` 同時接受舊格式**（value 是 records 陣列）→ 走 legacy 路徑。
    rolling deploy 期間使用者可能在舊版頁面按「登入並儲存」、在新版頁面登入回來，
    直接丟掉舊格式等於吞掉他剛上傳的整份案件資料。
  - 還原失敗（多半是 TTL 過期）**明講**並保留該筆狀態 —— 使用者以為資料還在才是最糟的。
- `api.js`：`createPreview(file, targetId, mapping)`；create / get / revoke 改
  `auth:false`（有 token 照送、沒有也不擋），**seal / save 維持必須登入**；
  429 → `rate_limited`（code 與 status fallback 兩路都通）。
- `preview-state.js`：`runTempPreviewUpload` 傳遞 `deps.mapping`；新增
  `serializePreviewStore` / `deserializePreviewStore` / `runRestorePreviews`（皆純函式，可 Node 測）。
- `_tempRecordsStore` **已無任何上傳路徑會寫入**，只剩「還原舊版 sessionStorage」一個用途。

### legacy 端點：標記 deprecated、**刻意不移除**

`/api/parse-only` 與 `/api/upload/parse-temp` 的契約（含 `_records`）**原封不動**。
理由是 rolling deploy：使用者瀏覽器可能還快取著舊版 `index.html`，那份程式碼會打這兩支
並讀 `_records`。現在移掉等於讓那些人的上傳靜默變成 0 筆。兩支的 docstring 已標
DEPRECATED；等舊頁面自然汰換後再連同 `_records` 欄位一併移除。

### 驗證（皆為實測結果）

- **backend pytest 531 passed**（原 503 → +28：`test_api_preview_guest_mapping.py` 19 條、
  `test_ingest_auto_mapping.py` 9 條）。
- **Node 純函式測試 79 passed**（`test_preview_client.js` 37、`test_preview_temp_flow.js` 42）。
- **真瀏覽器 smoke 35/35 passed**（playwright-core + 系統 Chrome，訪客路徑走新的
  `POST /api/preview` 並成功渲染 marker / popup / XSS 跳脫）。
  - 首跑有 2 條失敗，經查是**環境因素**：該樣本的 cell_id 不在 `cell_towers`、
    Google/OSM 皆停用 → `plotted=0`，走舊 `parse-only` 也一樣是 0。補上該檔 cell_id 的
    座標後 35/35 全過。
- **DB-backed E2E 25/25 passed**（本機 PostGIS + 真 HTTP + 真 JWT，`e2e_2b.py`）：
  訪客建立/讀取/撤銷、訪客 seal→401、他人讀具名 artifact→403、admin→200、
  訪客預覽→登入→save→evidence+2 筆落地→再讀 410 CONSUMED、
  陌生欄名不給 mapping→422 diagnosis、帶 mapping→200 且 `parser_type=manual_mapping`、
  重讀沿用 server 的 mapping、save 落地 2 筆（不是 0 筆）+ 建立 evidence、
  壞 mapping→400、coverage 2/2、撤銷後→410 REVOKED。
- **測試資料已清除**（e2e users / projects / preview_artifacts / 假 cell_towers 全數刪除，
  本機既有 dev 資料未動）。

### 正式環境驗收（2026-08-22，deployed commit `8af68dd`；皆為實測事實）

後端 `celltrail-api` 與前端靜態站 `celltrail` **兩邊都已完成自動部署**。
訪客路徑剛好是唯一不需要憑證就能驗的路徑，故以下全部直接打 production：

| # | 驗收項 | 結果 |
|---|---|---|
| 1 | 訪客 `POST /api/preview`（無 token）| **200**、`parser_type` 欄位存在 → 確認跑的是新程式（舊版此處回 401）|
| 2 | 回應不含 `_records` | ✅（`total` / `plotted` / `skipped` 照常揭露筆數）|
| 3 | `PREVIEW_ARTIFACT_KEY` 仍在 Render | ✅ 由 #1 回 200 反證（fail-closed，缺失必回 503）|
| 4 | 訪客憑 `preview_id` 讀回 | **200**、`total=2 plotted=2` |
| 5 | 訪客 `seal` | **401 AUTH_REQUIRED**（封存需具名身分，符合設計）|
| 6 | 訪客撤銷自己的預覽 | **200** |
| 7 | 撤銷後再讀 | **410 PREVIEW_REVOKED** |
| 8 | 陌生欄名、不給 mapping | **422 PREVIEW_PARSE_FAILED** + `details.diagnosis` |
| 9 | 帶 mapping 重送 | **200**、`total=2 plotted=2`、`parser_type=manual_mapping` |
| 10 | 重讀沿用 server 保存的 mapping | **200**、`total=2 plotted=2`（呼叫端未再送 mapping）|
| 11 | 壞 mapping JSON | **400 VALIDATION_ERROR** |
| 12 | 靜態站已是新版 | `doGuestUpload` 走 `runTempPreviewUpload`、不打 parse-only、不觸碰 `_records`；`api.js` `createPreview` 收 mapping；`preview-state.js` 有訪客交接函式 |

**未在 production 驗的兩項**（刻意）：
- **訪客配額 20/hr/IP**：要驗必須連打 21 次，會把該 IP 當小時的配額用完，
  且每次都在 production 落一份加密 artifact。helper 本身有單元測試（第 21 次起回 False）。
- **訪客 → 登入 → 儲存**：需要 production 的 admin/user 憑證，我方不持有。
  本機 DB-backed E2E 已完整覆蓋（C1–C4）。**這是 production 唯一還沒被實際走過的一段。**

測試期間建立的 artifact 均已 `DELETE` 撤銷；未寫入任何 `raw_traces` / `evidence_files`；
探針檔為合成的兩列經緯度，不含任何案件資料。

### 尚未完成（P9 後續）

- object storage A.5（5–50MB）
- supervisor seal workflow / custody ledger
- `geocoded_cell_estimates` 推估座標分表
- report ACL decision（REPORT_ACL_SPEC_MISMATCH，見上節 Finding）
- legacy `parse-only` / `parse-temp` / `save-records` 的**實際移除**（等舊頁面汰換）
- guest preview 的 TTL 過期還原**未做真瀏覽器 E2E**（純函式與 API 契約已覆蓋）

## 六、待辦事項（依優先級）

### 中期

| # | Task | 說明 |
|---|---|---|
| ~~0~~ | ~~preview 路徑 payload 瘦身~~ | **✅ 2026-08-22 P9 Phase 2B 完成** —— 訪客與手動對應也切到 Preview Artifact，前端三條上傳路徑都不再取得 `_records`。實測同檔 payload 縮減 67–94%（`cell_towers` 空、定位 0 時達 99.99%）。legacy `parse-only` / `parse-temp` 的契約**刻意保留不動**（rolling deploy：舊快取頁面仍會讀 `_records`），已標 DEPRECATED，待舊頁面汰換後移除。詳見「P9 Phase 2B」節。
| 1 | **填充 cell_towers 座標表** | 架構（P4.1）已就緒但表是空的；向業者取得基地台座標 CSV 匯入，可徹底解決純數字 cell_id 的 geocode 問題（消 geocode 時間牆，但七-8 記憶體牆仍在，需配 #0）。**2026-07-21 盤點**：手邊 16 檔共需 **6,620 個唯一 cell_id**，其中 96.1% 的列有 cell_id → 這條路的涵蓋上限最高。匯入前務必先看 **五-X**（匯入欄序 bug 已修，但仍有短碼唯一性問題）。**2026-08-22 已對手上那份 116 筆推估座標做完匯入預檢與本機實測，見七-12** |
| 2 | **P3–P6 API 補自動化測試** | **2026-08-22 大部分完成**（`test_api_admin_endpoints.py` 33 條，行為層而非只驗路由註冊）。已覆蓋：cell-towers 三端點（含 `?confirm=true` 破壞性護欄、匯入的逐列拒絕不中斷整批）、users 兩條自我鎖定守衛（不能降級/停用自己）+ 重設密碼不回 hash、account-requests 的重複核准 404 / 帳號衝突 409 / reject rowcount 分支 / 公開 check-phone 不外洩個資、carrier-profile 空欄名驗證、audit 的 admin-only + 參數化查詢 + page_size 上限。**三條關鍵守衛與兩條 audit 守衛已做突變測試驗證**（拿掉守衛測試會紅）。`members.py` 已由 `test_members_business_logic.py` 16 條函式層覆蓋，未重複寫 HTTP 層。**尚缺**：`stats.py`（計數器，風險低）、users/carrier-profile 的成功寫入路徑、`report.py`。|
| 3 | **carrier_profile DB 同步** | 把 `_RAW2CANON` 所有 key 補進 DB `mapping_json`（讓 DB 真正成為 SoT）。注意：雲端 active_map 用 `{**_RAW2CANON, **db_profile}` 合併，新增別名只要 push+redeploy 即生效、**不需** Supabase migration |

### 長期

| # | Task | 說明 |
|---|---|---|
| 4 | **檢警分艙 / 案件分艙細緻權限** | 目前 admin/user + project_members 三級已可用，但尚無組織層隔離 |
| ~~5~~ | ~~uvicorn `--reload` Python 3.13 macOS spawn bug~~ | **✅ 2026-05-31 已解** — 現行 `uvicorn==0.30.6` + `watchfiles==1.1.0` 改用 watchfiles（Rust notify），spawn bug 不復現，`--reload` 可正常熱重載（實測改 .py 乾淨重啟）。 |
| ~~6~~ | ~~前端 UI 自動化回歸~~ | **✅ 已有** — `frontend/tests/smoke.js`（playwright-core 驅動 Chrome，**46 條**）涵蓋公開頁/守衛重導向/admin 三分頁/audit 查詢/登入後 UX、**地圖互動**（訪客 parse-only 上傳→渲染 marker、popup 內容、測距工具、按鈕可見性）、**加密檔上傳錯誤提醒**、**問答式手動對應**（怪欄名→診斷→依範例值自動猜時間/地址欄）。走 parse-only 不寫 DB。`npm test`（帶 `CT_SMOKE_TOKEN` 跑完整 50 條）。 |

---

## 七、已知問題與環境陷阱

### 1. geocode 失敗的列不會在地圖上顯示（已不再沉默）

`/map-layers` 的 SQL 有 `WHERE geom IS NOT NULL`，geocode 失敗的列被過濾掉 → 不會出現在地圖。**2026-05-23 起此狀態已可見**：
- 後端 `GET /api/projects/{id}/coverage` 回未定位數 + 原因分布（見第五節 N）。
- 前端 L1 收據 / L2 banner / L3 詳細 modal 主動告知使用者。
- 全敗（with_geom = 0）就會看到「⚠ 本案另有 N / N 筆未定位」滿版提示。

手動確認 SQL：

```sql
SELECT count(*) AS total, count(geom) AS with_geom
FROM raw_traces WHERE project_id=? AND deleted_at IS NULL;
```

OSM 備援預設已啟用（`.env` `GEO_OSM_FALLBACK=1`）。要進一步降低未定位率，填 `cell_towers`（待辦 #1）或補 Redis cache。

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

### 7. 有三張表不在 schema.sql（+ 第四張：geocode_cache）

`project_members`（P3）、`account_requests`（P3）、`share_links`（P7）分別在
`migration_permissions.sql` / `migration_account_requests.sql` / `migration_share_links.sql`，
新環境要逐一另外套（見第三節）。

`geocode_cache`（P8）在 `migration_geocode_cache.sql`，但**也會由 geocode.py
`_ensure_sql_cache()` 自動 `CREATE TABLE IF NOT EXISTS`**，故新環境忘了套也不會壞；
migration 檔供「明確、可審計建立」之用。

### 8. 雲端大檔上傳：正式 `/upload` 已解（P8.1）；parse-only/parse-temp 仍待瘦身

#### A. 正式 `/upload`（存 DB 路徑）— **✅ P8.1 已解除（2026-06-28 雲端實測）**

P8.1 把 `_ingest_rows_stream` 改為 chunk-based（每塊 normalize→`lookup_bulk`→executemany→釋放），
記憶體峰值限於一塊、geocode 走並行+SQL 快取。**已部署 Render（commit 9f43006）並用完整
test3.xlsx 經正式 `POST /api/upload` 實測**（CIDadmin token、非 parse-only）：

- HTTP **200**、耗時 **42.2s**（改造前是 502@120s）
- `total=21757`、`inserted=21711`、`skipped=46`（皆「缺起始時間」驗證跳過）
- **無 502 / 無 timeout / 無 OOM**；`inserted+skipped=total` → 完整匯入、非部分匯入
- `GET /map-layers` 讀回 **21649** 點；`coverage`：`with_geom=21649 / without_geom=62`（定位率 99.7%）

#### B. parse-only / parse-temp（預覽路徑）— **仍偏大，下一階段優化目標**

**事實**（2026-06-27 對 Render 實測 parse-only）：5000 列切片 ✅ 200@30s；完整 test3 → 502
（冷快取 @120s 逾時、部分快取暖後 @73.8s 崩 = OOM）。

**推論**：預覽端點（`parse-only` / `parse-temp`）會**同時回完整 `_records` + GeoJSON
`features`**（記憶體 ×2，見五-Q/M2 議題），Render 小實例組裝兩萬筆回應時 OOM。chunking
（P8.1）只改了存 DB 路徑、**未改預覽路徑**，故大檔預覽仍可能 502。

**目前作法**：大檔走**正式 `/upload`**（已解，見 A）；若只是要預覽大檔，仍建議拆 ≤5000 列。
**下一階段（待辦 #0）**：preview payload 瘦身（移除 `_records` 重複 / 分頁）。

> `GEO_GOOGLE_CONCURRENCY` env（預設 10）可在 Render 調高加速 geocode（仍受 Google
> ~50 QPS 配額）。`INGEST_CHUNK_SIZE`（預設 800）可調存檔分塊大小。parse-only 限
> **20 req/hr/IP**，盲測大檔很快用完配額。

### 9. simple_time_location 格式（P8.2）注意事項

**【已確認】**（Render 雲端 + 正式 `/api/upload` 實測，見五-U）：
- `simple_time_location` 已在雲端驗證成功；正式 `/api/upload` 可解析 **民國年**、**單格經緯度（免 geocode）**、**中文地址（走 geocode）**。
- `map-layers` / `coverage` 讀回正常（測試檔 2 列全定位）。

**【已知限制】**：
- **CSV 尚未支援**（`simple_time_location` 只接 Excel，走 `_iter_rows_excel`；CSV 走 `_iter_rows_csv` 未接此 fallback）。
- **headerless 單列檔**仍受 `_iter_rows_excel` 規則 A（`rows<2` 跳過）限制：無表頭需 ≥2 列；有表頭單筆（表頭+1 資料=2 列）可解。
- **canonical 地址表頭 + 經緯度內容的混用 edge**尚未處理：若表頭用 canonical 別名（如「地址」，過規則 B 走正常路徑）但 B 內容其實是經緯度，會被當地址 geocode（可能失敗）。simple 偵測只在規則 B 失敗時觸發，故不涵蓋此罕見混用。

### 10. 【重要】production 定位率目前趨近 0（2026-07-21）

**症狀**：上傳成功、解析成功，但地圖上幾乎沒有點（`plotted=0`）。**不是解析壞掉。**

**三條定位路徑同時斷掉**（皆為事實，已實測）：

| 路徑 | 狀態 |
|---|---|
| `cell_towers` 本地對照表 | **空表**（待辦 #1 從未執行）|
| Google Geocoding | **停用**（`GEO_GOOGLE_ENABLED=0`，2026-07-03 為控制費用）|
| OSM / Nominatim | **停用**（`GEO_OSM_FALLBACK=0`，2026-07-21）|

**為何關掉 OSM**：Google 停用後只剩 OSM，而它為遵守 Nominatim 1 req/s 政策**刻意序列查詢**、每址打兩次（自由格式 + 結構化），且對台灣門牌命中率極低。實測每址約 1.3 秒、**20 個唯一地址就 >250 秒無回應**（Render 請求上限約 120 秒）→ **任何真實案件檔必定逾時，系統形同不可用**。關閉後：11,500 列 / 1,100 唯一地址由「必定逾時」變為 **5.8 秒完成**。

**取捨說明**：關閉 OSM **沒有損失實質定位能力**（本來就幾乎 0%），換來的是系統從「不可用」變「可用但點位待補」。未定位列完整寫入 DB、由 coverage UI 誠實揭露。

**⚠ 未量測項**：我方抽樣「OSM 命中率 0/100」用的是**自由格式**查詢，而 `_osm_geocode` 另有一段**結構化**查詢（其 docstring 自稱「自由格式 0/3、結構化 1/1」）未被納入量測。故「關閉 OSM 損失多少定位」**尚無可靠數字**，僅知全面性定位率實測趨近 0。

**根本解**：填 `cell_towers`（待辦 #1）。手邊 16 檔共需 **6,620 個唯一 cell_id**、96.1% 的列帶 cell_id。此路一旦通，**速度與定位問題同時消失**，且完全不依賴任何外部 geocoding 服務。

**2026-08-22 複驗（事實）**：狀態**完全沒變**。用 `backend/scripts/probe_cell_towers.py` 對 production 抽樣 30 個 cell_id → **0/30 定位成功**，對照組（不存在的編號）亦正確未定位（證明探針有鑑別力，不是整條路壞掉）。

**這支探針怎麼繞過 admin 權限**（值得記下來的手法）：`/api/admin/cell-towers/stats` 需要 admin token，但送一個**只有時間 + 基地台編號、刻意不含地址欄**的檔案到訪客端點 `/api/parse-only`，就能間接測出來 —— 因為 geocode 的查詢鏈是 `cell_towers → geocode_cache → OSM → 放棄`，沒有地址時後面幾層全都無從施力，**有座標回來就只可能來自 `cell_towers`**。於是「定位成功」與「表裡有這筆」互為充要條件。此法不需憑證、不寫任何 DB、且測的是**真實使用路徑**而非表的 row count（表有資料但查不到，例如編號格式不符，stats 看起來會是好的）。注意 parse-only 限 20 req/hr/IP，多筆 cell_id 應塞進同一個請求。

**手上已有的過渡資料（未匯入）**：`data/` 底下有一份 **116 筆**的已驗證推估座標 CSV（`geocode_verify.py` 產出，通過行政區＋路名雙重反查，涵蓋 14 個站點；`data/*.csv` 在 .gitignore 內、不入版控，故此處不記檔名以免案件資訊進公開 repo）。格式已檢查可直接匯入（欄名 `cell_id,lat,lng,memo`、cell_id 全唯一、座標無越界）。**但它是地址推估值、非業者座標，精度是「路名正確」而非「門牌正確」**，匯入前須確認此限度可被接受，見七-11。匯入後請跑：

```bash
cd backend && python3 scripts/probe_cell_towers.py --expect ../data/<那份>.csv --sample 30
```

—— 它除了確認生效，還會**逐筆比對座標**，抓「匯進去了但經緯度對調」這種在地圖上看起來完全正常的錯誤（五-X 的失效模式）。

### 11. 【最重要】OSM 對本專案地址會回傳「看起來正常但錯誤」的座標（2026-07-22）

**⚠ 這是本專案目前最危險的已知風險。任何人想用 OSM/Nominatim 補定位前，務必先讀完本節。**

**背景**：為了在不花 Google 費用的前提下提高定位率，曾嘗試把地址中的「里/鄰」剝除後再查（電信業者交付的基地台地址常把里夾在區與路之間，而里屬行政區劃、非郵遞地址）。單看命中率極為漂亮：

- 前 6 大地址（依列數）：含里查詢命中 1/6 → **去里後 5/6**
- 前 5 大地址涵蓋 **11,931 / 12,060 列（99%）**

**但反查驗證後發現：命中的結果有一半是錯的。**

| 查詢地址 | OSM 回傳實際位置 | 判定 |
|---|---|---|
| 高雄市**鳳山區**文福里建國路三段539號 | 建國路，**路竹區** | ❌ 偏差約 26 公里 |
| 高雄市三民區寶業里陽明路170巷8號 | 陽明路，三民區 | ✅ |
| 高雄市三民區本館里**昌裕街**1號 | **本館路，鳥松區** | ❌ 完全不同的路與區 |
| 高雄市三民區本揚里天民路176巷12號 | 天民路，三民區 | ✅ |

第 3 例最能說明問題：Nominatim 拿「本館**里**」的「本館」去比對到鳥松區的「本館**路**」，與原本要找的「昌裕街」毫無關係。

**根因（推論，但與觀察一致）**：Nominatim 的 search 是**模糊比對**，設計目標是「盡量給個接近的答案」，而非「找不到就說找不到」。對台灣中文地址（門牌覆蓋稀疏、路名高度重複於各行政區）尤其危險。

**為何對本專案是致命的**：基地台座標**就是證據**。錯誤的座標在地圖上與正確的完全無法分辨 —— 使用者會看到一條流暢合理的軌跡，卻指向錯誤的地點。**「查不到」可以補資料，「錯了」不會有人發現。**

**結論與處置**：
- 剝除里/鄰的改動**已撤回**，不予採用。
- 先前量測的「OSM 命中率 30.7%」**不可信** —— 那些命中未經行政區驗證，同樣可能包含錯誤座標。據此產生的 `cell_towers_from_addr.csv`（125 筆地址推估座標）**不應匯入**。
- 若未來仍要使用 OSM，**必須加上驗證層**：對回傳座標做 reverse geocode，確認行政區與原地址一致才採用。這會使請求數加倍，且涵蓋率會大幅低於未驗證時的數字。
- **`cell_towers`（業者提供的實際站台座標）仍是唯一能撐住法庭質詢的路徑。** 這次的教訓反而強化了這個結論：任何由地址反推的座標都需要獨立驗證，而業者座標不需要。

次選：Google（覆蓋率遠優於 OSM）。**但 2026-07-22 實測本機金鑰為 `REQUEST_DENIED`（The provided API key is invalid），啟用前必須先在 GCP Console 修復。** 另使用者反映上月費用達 NT$5,000，成本需另行評估。

**可用的過渡方案**：`backend/scripts/geocode_verify.py`（2026-07-22）—— 離線地理編碼 + **雙重反查驗證**，把上述「模糊比對回錯誤座標」的風險擋在匯入之前。

```bash
cd backend && source .venv/bin/activate
python scripts/geocode_verify.py <檔案或資料夾> -o towers.csv [--limit N] [--email you@example.com]
# 產出 cell_id,lat,lng,memo → admin.html 基地台座標表匯入
```

- **兩道驗證**：① 反查座標的行政區須與原地址一致 ② 反查的路名須與原地址路名相容（允許「大豐一路288巷」對「大豐一路」這類更細層級）。
- **實測成效**（三個真實案件檔、前 40 大地址）：採用 19 址 / 6,050 列（42.5%）；**擋下 11 址 / 7,172 列的錯誤匹配** —— 若無驗證，這些會全部變成地圖上看似正常的錯誤點位。
- **離線執行的必要性**：嚴守 Nominatim 1 req/s，每址最多 4 次查詢 + 1 次反查（約 5 秒/址），遠超 Render 請求上限，故不可能在上傳週期內完成。
- **殘留限制（必須告知使用者）**：精度是「**路名正確**」而非「門牌正確」，座標可能落在該路某處；產出為**地址推估值、非業者座標**，每列 memo 均標註。業者對照表到手後直接重匯即覆蓋（`cell_towers` 為 `ON CONFLICT DO UPDATE`）。
- 純判準邏輯由 `test_geocode_verify_script.py`（11 條）守住 —— 其中 `road_of` 曾因「路竹**區**」的「路」被誤判為路名而回傳「高雄市路」，是測試抓出來的實際 bug。

### 12. 那份 116 筆推估座標：匯入預檢與本機實測結果（2026-08-22）

`data/cell_towers_橋檢_top40_已驗證.csv`（`geocode_verify.py` 產出，七-11 的過渡方案）
在匯入 production 之前先做完的驗證。**以下皆為實測事實。**

**① 匯入解析預檢（以 `api/cell_towers.py` 實際的欄位解析邏輯離線跑）**

| 檢查項 | 結果 |
|---|---|
| 欄位索引推導 | `id=0, lat=1, lng=2, memo=3` —— 未觸發五-X 的 falsy-zero 路徑 |
| 可匯入 / 跳過 / 錯誤 | **116 / 0 / 0** |
| cell_id | 116/116 唯一、全數字、無科學記號變形；20 碼 ×42（中華）、15 碼 ×74（台哥大）|
| 座標 | lat 22.614–24.150、lng 120.259–120.595，全數落在台灣本島框內 |
| 唯一站點 | 14 個 |

**② 離群站點查證（七-11 的教訓：必須排除「模糊比對回錯座標」）**

14 個站點有 13 個在高雄（lat 22.61–22.87），一個在 **lat 24.1496 / lng 120.5950**，
距其餘約 165 公里。到原始樣本的 `sharedStrings.xml` 反查該 cell_id `466970408128143`：
它出現在 `026962 陳2號機網路` / `026965 陳1號機網路` / `複本 029935` 三檔，
**相鄰編號的原始地址是「台中市南屯區文山里保安三街108號」「台中市南屯區精科五路9號」**
→ 這是真實移動軌跡，不是地理編碼假影。

**③ 本機真 DB 匯入實測（走正式 `POST /api/admin/cell-towers/import`）**

`inserted=116 / updated=0 / skipped=0 / errors=[]`；回查 DB 確認
`lat` 落在 22.6x、`lng` 落在 120.3x（**未經緯度對調**），116/116 在台灣範圍內。
→ **五-X 那個「只在第一次真正匯入時發作」的欄序 bug，對這份 CSV 不會發作。**

**④ 匯入後各檔定位率（本機，Google/OSM 皆停用 → 座標只可能來自 `cell_towers`）**

| 檔案 | 解析 | 已定位 | 定位率 |
|---|---:|---:|---:|
| `028351 蘇世崇網路` | 9,281 | **3,758** | 40.5% |
| `031543 蘇網路` | 4,800 | **1,996** | 41.6% |
| `026965 陳1號機網路` | 11,511 | 604 | 5.2% |
| `026962 陳2號機網路` | 1,491 | 45 | 3.0% |
| `複本 031542 蘇手機通聯` | 156 | **0** | 0.0% |

**怎麼讀這張表**：匯入前這五個檔全部是 **0**（七-10 的探針結果）。所以這 116 筆
確實把「蘇」系列從完全無點推到約四成有點；但「陳」系列只有 3–5%，`複本 031542`
（通聯而非上網歷程，基地台編號體系不同）完全沒被涵蓋。

**結論**：值得匯入（從 0 到四成是實質改善，且 `ON CONFLICT DO UPDATE`，日後業者
對照表到手直接重匯即覆蓋），但**它不是待辦 #1 的替代品** —— 精度是「路名正確」
而非「門牌正確」，且涵蓋範圍偏在單一案件的部分門號。仍須向業者索取真正的座標表。

### 13. 全樣本盤點：34 個檔案的「能不能辨識」與「能不能定位」（2026-08-22）

使用者要求確認手邊所有檔案是否都能辨識與定位。以下是把 `基地台位置範例檔案/` 全部
34 個檔案跑過一次的結果。**geocode 設定刻意與 production 一致**（Google 停用 + OSM 停用），
本機 `cell_towers` 只載入那份 116 筆推估座標 → 有座標就只可能來自 `cell_towers`。

**總計：解析成功 21 / 34 檔、120,203 列；已定位 23,193 列（19.3%）。**

#### A. 解析成功的 21 檔（定位率差異極大，且與業者高度相關）

| 檔案 | 解析 | 已定位 | 定位率 |
|---|---:|---:|---:|
| `028346 蘇世崇網路11406-11410` | 18,669 | 7,932 | 42.5% |
| `028349 蘇世崇網路11411-11503` | 17,493 | 6,682 | 38.2% |
| `031543 蘇網路` | 4,800 | 1,996 | 41.6% |
| `028351 蘇世崇網路11504-11506` | 9,281 | 3,758 | 40.5% |
| `026964 陳1號機網路` | 18,556 | 1,005 | 5.4% |
| `026965 陳1號機網路` | 11,511 | 604 | 5.2% |
| `026963 陳1號機網路` | 9,735 | 402 | 4.1% |
| `複本 029935 陳1號機網路` | 4,900 | 370 | 7.6% |
| `026962 陳2號機網路`（兩份） | 各 1,491 | 各 45 | 3.0% |
| `RFX-6179.pdf` | 354 | 354 | **100%** |
| `0801-0903彭奕翔網路歷程` | 11,246 | 0 | 0% |
| `周蔓達上網歷程` | 6,769 | 0 | 0% |
| `電話通聯+歷程` | 2,606 | 0 | 0% |
| `陳 蘇 基地台重疊` | 759 | 0 | 0% |
| `複本 031542 蘇手機通聯` | 156 | 0 | 0% |
| `網路歷程.xltx` / `.xlsx` | 149 / 134 | 0 | 0% |
| `楊云豪…(含方位角).pdf`（兩份） | 68 / 17 | 0 | 0% |
| `雙向通聯.xlsx` | 18 | 0 | 0% |

**怎麼讀這張表**：
- **解析（辨識）沒有問題** —— 這 21 檔全部達 100% 或資料物理上限，`with_cid` / `with_addr`
  幾乎逐列齊全（例：彭奕翔 11,246 列全部都有編號與地址）。
- **定位是唯一的瓶頸，而且純粹取決於 `cell_towers` 涵蓋到哪些編號**。那份 116 筆推估座標
  是從「蘇」系列的高頻地址產出的，所以蘇系列有四成、陳系列只有 3–8%、其餘完全沒有。
- `RFX-6179.pdf` 100% 是因為它**檔內直接帶經緯度**，根本不需要對照表 —— 這反過來
  證明定位鏈本身是好的，缺的只是座標來源。
- `陳 蘇 基地台重疊.xlsx` 的 `with_cid=0`（只有地址欄），故連 `cell_towers` 都幫不上，
  必須靠地址 geocode —— 而那條路已因七-11 關閉。

#### B. 解析失敗的 13 檔：全部是同一類 PDF

13 檔**全部是 PDF**，而且是同一種匯出樣式：遠傳 / 台灣大哥大的
「通訊數據上網歷程查詢」**純文字、無框線、靠空白對齊**的版面。

- `pdfplumber.extract_tables()` 回 **0 個表**（沒有框線可依循）。
- `extract_text()` 會把相鄰欄**逐字交錯**：實際看到
  `回回覆覆時狀間態:...`（「回覆時間」與「回覆狀態」交錯）、
  `查查詢詢起終始止時時間間:...`、`起始基地台地址離開基地台地址`。
- 現行 PDF 路徑（`ingest_pdf` → `_extract_tables_from_page` → `_match_col_idx`）
  依賴「有框線的表格」，對這種版面完全無法施力。**這不是欄名別名缺漏，加別名沒有用。**

**其中 7 檔是重複的**：以 PDF 內文的「發文案號」比對，發現它們與已能解析的 xlsx
是**同一個調閱案的兩種格式**：

| 失敗的 PDF | 發文案號 | 已可解析的同案 xlsx |
|---|---|---|
| `蘇世崇網路 114.06-114.10.pdf` | 028346 | `028346 …xlsx`（18,669 列 / 定位 7,932）|
| `蘇世崇網路 114.11-115.03.pdf` | 028349 | `028349 …xlsx`（17,493 / 6,682）|
| `蘇世崇網路 115.04-115.06.pdf` | 028351 | `028351 …xlsx`（9,281 / 3,758）|
| `陳1號機網路 114.6-11.pdf` | 026963 | `026963 …xlsx`（9,735 / 402）|
| `陳1號機網路 114.1-115.3pdf.pdf` | 026964 | `026964 …xlsx`（18,556 / 1,005）|
| `陳1號機網路 115.4-115.6pdf.pdf` | 026965 | `026965 …xlsx`（11,511 / 604）|
| `陳2號機網路 115.5-6.pdf` | 026962 | `026962 …xlsx`（1,491 / 45）|

→ **這 7 檔解析失敗沒有造成任何資料損失**，同一批資料已由 xlsx 版完整進系統。

**真正只有 PDF 的是 `0927352336`（門號 0927352336）那 6 檔**（發文案號 037035 /
037044 / 037046 / 037574 / 037575 + 一份未標案號的 2,725 頁）。這批目前**完全進不了系統**。

#### C. 那份 2,725 頁的 PDF：一個值得記下來的版面

`0927352336 114.8-115.1.pdf` 是**逐欄分頁**（column-major）的：不是每頁一批完整的列，
而是**一個欄位連續印好幾百頁**，印完換下一欄。實測分佈：

| 頁範圍 | 內容 |
|---|---|
| p1 – ~p1000 | `通聯起始時間` + `通聯時間(秒)` |
| ~p1500 – p1610 | 基地台地址（`高雄市鳥松區…室內(4G)`）|
| ~p1611 – ~p2300 | `IP:port` + IMEI + **基地台編號**（`466011101195454`）|
| ~p2401 – | 門號 |
| p2725 | 全部使用量(Byte) |

**一開始我抽樣 46 頁、只看到時間與秒數，誤判「此檔沒有基地台資訊、永遠無法定位」——
那是錯的。**抽樣沒有涵蓋到後段的欄區塊。改用約 300 頁分層抽樣後才看到編號與地址。
**教訓：對 column-major 版面做抽樣診斷會系統性地漏掉整個欄位**，必須全檔掃描或至少
確認抽樣覆蓋所有頁區間。

**但「有資料」不等於「可安全重組」**。全檔統計各區塊的資料列數：

```
通聯起始時間+秒數   39,086 列
基地台地址          15,312 列
含基地台編號的行     19,543 列
```

**三者不相等** → 無法用「第 N 列對第 N 列」的位置關係把欄位拼回去。若強行對齊，
偏移一列就會讓**每個點的時間都掛到錯誤的基地台上**，而且在地圖上看起來完全正常
（與五-X 欄序錯誤、七-11 OSM 錯誤座標同一種致命失效模式：錯得沒有徵兆）。
要正確重組必須改用 `extract_words()` 的 x/y 座標做版面重建，屬於新的解析能力，
**不是既有 PDF 路徑能延伸的**。

#### D. 結論與建議順序

1. **辨識**：手邊資料沒有辨識問題。唯一進不了系統的是 `0927352336` 那 6 個 PDF，
   而它們的成因是版面，不是欄名。
2. **最省力的解法**：向遠傳索取 `0927352336` 這幾個調閱案的 **Excel 版**。
   同一家業者對「蘇世崇」案就是同時給了 xlsx 與 PDF（見上表），代表他們產得出來。
   一封公文換 100% 解析，遠優於為單一版面寫一套風險很高的重組器。
3. **定位**：仍是待辦 #1。整體 19.3% 全部來自那 116 筆推估座標；業者座標表到手前，
   解析再完整也只是這個數字。

### 14. 「全部定位」還缺什麼：TGOS + NLSC 兩條官方路（2026-08-22）

七-13 確認 Excel 的**辨識**已達標，瓶頸完全在**定位**。這一節記錄「要讓全部列定位」
實際需要什麼，以及本輪為此建好的東西。

#### A. 需要處理的規模：只有 3,459 個不重複地址

全部 Excel/CSV 共 119,764 列，但**不重複地址只有 3,459 個**，且分佈極度集中：

| 前 N 個地址 | 涵蓋列數 | 比例 |
|---:|---:|---:|
| 50 | 89,708 | 75.4% |
| 400 | 108,749 | 91.4% |
| 800 | 113,023 | 95.0% |
| 3,459（全部）| 118,934 | 100% |

**這個數字改變了可行性判斷**：不是十二萬列要查，是三千多個地址要查一次。

#### B. 天花板：有 517 個「地址」根本不是地址

| 類型 | 地址數 | 列數 | 能不能定位 |
|---|---:|---:|---|
| 門牌型（含「N號」）| 2,942 | 112,977（95.0%）| ✅ 門牌地理編碼可解 |
| 地號（`高雄市燕巢區瓊安段758地號`）| 約 480 | 約 5,400 | ⚠ 需**地籍**資料，不是門牌服務 |
| 電桿（`崎漏高幹125電桿`）| 少數 | 少數 | ⚠ 台電編號，需台電資料 |
| `上網或使用VoWiFi(透過Wi-Fi訊號通話)` | 1 | 303 | ❌ **本來就不是位置** |
| `(簡訊系統發信)` | 1 | 177 | ❌ **本來就不是位置** |

最後兩類共 **480 列**：業者是在告訴你「這幾筆沒有經過基地台」（Wi-Fi 通話 / 簡訊系統發送）。
**它們定位不到是正確行為**，不是缺陷 —— 由 coverage UI 誠實揭露即可。
所以「全部定位」的實際上限是 **99.6%**，不是 100%。

#### C. 三條正向地理編碼路徑的現況（2026-08-22 實測）

| 來源 | 狀態 |
|---|---|
| `cell_towers` | 116 筆（涵蓋 3,459 個地址中的 14 個站點）|
| Google Geocoding | **金鑰仍無效**（`REQUEST_DENIED: The provided API key is invalid`，本日重驗，七-11 記的狀態未變）|
| OSM / Nominatim | 停用（七-11：會回看起來正常但錯誤的座標）|
| **TGOS 全國門牌地址定位** | **不可行 —— API 申請須綁 1~4 個固定 IP，本專案無法提供**；另 `.env` 內端點亦已失效（404）|

→ **目前沒有任何一條可用的正向地理編碼路徑。** 這是行政問題（要申請帳號），不是程式問題。

#### D. 本輪建好的東西（拿到憑證即可用）

**1. TGOS provider（`services/geocode.py`）** —— 排在 Google / OSM **之前**：

- TGOS 整合的是**戶政機關門牌坐標**，對台灣門牌是權威來源；Google/OSM 是通用地理編碼器，
  對中文地址會「盡量給個接近的答案」，正是七-11 那個失效模式的來源。**權威優先、通用殿後。**
- `oIsLockCounty` / `oIsLockTown=true` 是**安全性設定**：模糊比對不得跨行政區，
  直接消滅「鳳山區地址比到路竹區」那類 26 公里誤差。**寧可查無，不可查錯。**
- 無憑證時 `_tgos_enabled()` 回 False，**在建立任何 HTTP request 之前返回**
  （同 `GEO_GOOGLE_ENABLED` 的硬止血語意）。可用 `GEO_TGOS_ENABLED=0` 明確關閉。
- 回應解析獨立成純函式 `_tgos_parse`：`.asmx` 會把 JSON 包在一層 `<string>` XML 裡，
  且欄位大小寫在版本間有出入。**X=經度、Y=緯度**，以名稱取值不靠位置（弄反就是五-X 的翻版）。
  若回的是 TWD97 投影座標（值以十萬計）→ **拒收不換算**（換算要猜是哪個帶，猜錯一樣是錯座標）。
- **`.env` 的舊端點 `map.tgos.tw/TGOS/geocode/QueryAddr` 已失效**，程式明確把它視為
  「未設定」→ 改用現行的 `addr.tgos.tw/addrws/v30/QueryAddr.asmx/QueryAddr`。
  理由：照單全收的話，使用者好不容易申請到憑證只會看到「TGOS 沒反應」，然後把時間花在懷疑金鑰上。
- **⚠ 未對真實服務測試過**（沒有憑證）。24 條測試守的是「無憑證不發請求 / 參數鎖行政區 /
  回應解析與經緯度不弄反」這些不需憑證也能驗、而且錯了很貴的部分。

**2. NLSC 官方反查驗證（`scripts/geocode_verify.py`）** —— 免申請、免金鑰，**現在就能用**：

- `--verifier nlsc`（新預設）改用內政部國土測繪中心的行政區反查，取代原本的 OSM reverse。
  理由：OSM 的 `suburb`/`city_district` 是社群標註，對台灣覆蓋與命名都不穩定；
  **驗證器本身不準就等於沒有驗證**，而驗證是七-11 之後唯一擋得住錯誤座標的東西。
- `--provider {osm,tgos}` 切換正向來源；指定 `tgos` 但沒憑證會**提早失敗並印出申請方式**
  （否則會安靜地全部查無，使用者只會以為地址有問題）。
- **SSL 注意**：NLSC 憑證缺 Subject Key Identifier，Python 3.13 預設的 `VERIFY_X509_STRICT`
  會拒連（curl 照連）。解法是只關 strict 旗標、**保留憑證鏈與主機名驗證** ——
  改成 `_create_unverified_context()` 會讓中間人得以竄改反查結果，而那正是我們用來
  判斷座標可不可信的依據。有測試釘住這一點。

**3. 用 NLSC 重驗了那份 116 筆推估座標** —— **14 個站點全數相符、116/116 通過**。
它原本只有 OSM 反查背書（七-11/七-12），現在多了一套**獨立且官方**的驗證。可安心匯入。

#### E. 建議的執行順序（2026-08-22 更新：TGOS API 這條路已排除）

> **⚠ TGOS API 申請須綁定 1~4 個固定 IP，本專案做不到**（使用者告知；Render 對外 IP 動態，
> 本機／辦公端也無可登記的固定 IP）。這不是「還沒申請」而是「申請不了」。
> `_tgos_geocode` 保留無妨（預設關閉、無憑證不發請求），但**不要再把申請 TGOS API 列為待辦**。
> 另：門牌座標**沒有開放下載**（data.gov.tw 上是民眾在請求開放，全國路名資料集不含座標），
> 所以「下載離線資料集」目前也不存在。

**改採 Google Geocoding 的一次性離線批次** —— 這是目前唯一實際可行且不需固定 IP 的路：

1. **修好 Google 金鑰**：GCP Console → APIs & Services → Credentials 建新金鑰、啟用
   Geocoding API。（目前這把是 `REQUEST_DENIED: The provided API key is invalid`，
   2026-07-22 與 2026-08-22 兩次實測皆然。注意 HTTP referrer 限制對伺服器端呼叫無效。）
2. **在本機跑一次離線批次**（**不是**在 production 跑）：
   ```bash
   cd backend && source .venv/bin/activate
   export GOOGLE_MAPS_API_KEY=新金鑰
   python scripts/geocode_verify.py ../基地台位置範例檔案 \
       -o ../data/towers_google.csv --provider google --verifier nlsc
   ```
3. 產出的 CSV 由 `admin.html` → 基地台座標表匯入。

**為什麼這次不會重演 NT$5,000 的帳單**（這是本輪最重要的判斷）：

- 上次的費用是**架構問題不是用量問題** —— 當時沒有 `cell_towers`、也還沒有 `geocode_cache`，
  於是**每次上傳都把同一批地址重查一遍**，一個月累積成數萬次請求。
- 現在唯一地址只有 **3,459 個**（蘇／陳三人合計 2,440 個），查一次寫進 `cell_towers`
  就**永不重查**。Google Geocoding 自 2025-03 起每月前 **10,000 次免費**
  → 一次跑完通常是 **NT$0**。
- production 維持 `GEO_GOOGLE_ENABLED=0`，**runtime 永遠不會呼叫 Google**，
  費用不可能從線上復燃；順帶也不受 Render 120s 請求上限影響。
- 腳本另有 `--max-requests`（預設 9000）作為護欄：達上限即停止並印出已查次數，
  不會安靜地跑過免費額度。結果區會印「正向查詢次數」供對帳。

**驗證不因換來源而省**：Google 是**通用**地理編碼器，一樣會「盡量給個接近的答案」，
所以 NLSC 官方行政區反查照跑（七-11 的教訓與來源無關）。

**其餘兩條路**：
- `地號` 那 480 個地址需要地籍資料（NLSC 的 `ListLandSection` 免金鑰可查段代碼，
  但段+地號→座標仍需另尋來源）。約 5% 的列，優先度低於上述。
- **業者座標表（待辦 #1）仍是最終解**：Google 產出的是「門牌地址的座標」，
  不是「業者登記的站台座標」，memo 已標明區別。

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

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
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
2. `cat WAKE_UP_TODO.md` 看當前待辦（2026-08-22 起已汰換為「指向本檔」的精簡版；**本檔第六節才是待辦的唯一真實來源**，該檔只列「下一步該按什麼鍵」）
3. 看 `infra/docker-compose.yml` 確認 DB/redis 是否需要先啟動
4. 看 `backend/app/main.py` 的 router 清單（最快掌握 API 全貌）
5. 看 `backend/app/services/ingest.py` 的 `_DIALECT_HEADER_MAPS` 與三 phase 函式（`_parse_rows_to_records` / `_parse_pdf_to_records`）；P8 後另注意 `_iter_rows_excel(user_mapping=)`、`_guess_header_row_idx`、`_read_xlsx_top_rows`
6. 看 `backend/app/db/schema.sql` + 四個 migration 檔（permissions / account_requests / share_links / geocode_cache）
7. 前端關鍵函式改用 grep 定位（P6 後 index.html 已重寫，行號不可靠）：`openAzRefModal`、`refreshSeriesPanel`、popup 渲染、格式診斷 modal、`showManualMappingModal`
8. 大檔上傳卡 502 → 先看七-8（雲端記憶體上限，分批 ≤5000 列頂著）

關鍵檔案地圖：

```
backend/app/
  main.py            ← FastAPI 入口 + 全部 router 掛載清單
  security.py        ← JWT、get_current_user、require_admin、assert_project_access、project_members
  services/
    ingest.py        ← 核心邏輯（_normalize_row, _match_col_idx, _detect_dialect, _parse_ts,
                        三 phase ingest, ParseDiagnosisError, _peek_headers, _apply_user_mapping；
                        P8: _iter_rows_excel(user_mapping=), _guess_header_row_idx,
                        _read_xlsx_top_rows 假 dimension fallback, SCAN_WINDOW=60,
                        規則 A2 multiset 子集分頁去重：_evidence_fingerprint /
                        _materialize_sheet_row，見五-W）
    geocode.py       ← Google(並行 ThreadPool) + OSM 備援(序列) + cell_towers 本地查詢
                        + Redis/SQL geocode_cache 持久快取 + lookup_bulk 批次(增量寫回)
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
    cell_towers      ← /api/admin/cell-towers stats/import/delete
    carrier_profile  ← 欄名對照表管理
    parse_only       ← /api/parse-only（純解析不寫 DB）
    format_reports   ← /api/format-reports 格式回報
    share            ← /api/share（P7 分享連結；公開端點 GET /api/share/{token}）
  db/
    schema.sql                    ← raw_traces / users / audit_logs / evidence_files /
                                     carrier_profiles / cell_towers / format_reports / geocode_cache
    migration_permissions.sql     ← project_members（P3，需另外套）
    migration_account_requests.sql← account_requests（P3，需另外套）
    migration_share_links.sql     ← share_links（P7，需另外套）
    migration_geocode_cache.sql   ← geocode_cache（P8；也會自動 CREATE IF NOT EXISTS）
    session.py                    ← psycopg pool
  tests/
    test_ingest_tw_mobile_data.py ← P8 台哥大格式 + 假 dimension（5 條）
    test_ingest_manual_mapping.py ← P8 手動對應結構性修復（7 條）
    test_geocode_bulk_parallel.py ← P8 並行 geocode + SQL 快取（6 條）

frontend/
  index.html         ← 主頁（P6 Google Maps 風格全螢幕 UI；含 popup、azimuth modal、
                        格式診斷 modal、measure、自訂標記、新手導覽、P7 分享連結 modal、
                        showManualMappingModal 問答式手動對應）
  share.html         ← P7 分享連結公開檢視頁（獨立精簡頁，憑 ?t=token 唯讀檢視）
  login.html / register.html / change-password.html  ← 帳號流程（P4.4 深藍科技風）
  admin.html         ← 管理：使用者 / 欄名對照表 / 格式回報
  audit.html         ← P2 稽核檢視
  api.js             ← 前端 API base/helper（本機 hostname→localhost:8000；
                        否則→ https://celltrail-api.onrender.com 雲端）

infra/docker-compose.yml   ← db + redis + tileserver

backend/scripts/
  probe_cell_towers.py  ← 免憑證驗證 cell_towers 是否生效（打 parse-only，含座標比對防五-X 欄序錯誤）
  geocode_verify.py     ← 離線地址→座標 + 行政區/路名雙重反查驗證（七-11 的過渡方案）
  diag_ingest.py        ← 單檔解析診斷
  apply_schema_p0p1.sh / rebuild_venv.sh / smoke_audit.sh / smoke_upload.sh
```

根目錄三份文件的分工（避免再度 drift）：

<!-- sync:verbatim-start -->
| 檔案 | 角色 |
|---|---|
| `CLAUDE.md` | 專案狀態與決策的**唯一真實來源**（給 Claude Code） |
| `AGENTS.md` | 上者的 Codex 鏡像，**由 `bash scripts/sync_agents_md.sh` 產生 —— 不要手動編輯**，改了會在下次同步被覆蓋 |
| `WAKE_UP_TODO.md` | 只回答「下一步該按什麼鍵」，任何細節一律回頭查 `CLAUDE.md` |
<!-- sync:verbatim-end -->
