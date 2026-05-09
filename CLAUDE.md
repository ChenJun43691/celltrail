# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 最近一次更新：2026-05-09（W2/P2.5 milestone 系列完成）

---

## 一、專案是什麼

**CellTrail**：刑事偵查用的「基地台連線歷程」匯入 + 視覺化系統。

**使用者**：刑事警察（Project owner: 高市刑大冠鈞 chen95572295@gmail.com / e43691@kcg.gov.tw）。

**核心流程**：
1. 偵查員拿到電信業者交付的歷程檔（CSV/Excel/PDF）
2. 上傳到系統 → 後端解析 → DB
3. 地圖視覺化：基地台位置 + 精度圓 + 方位角扇形
4. 法庭可防禦性：軟刪、audit_logs、SHA-256 證據鏈、方位角北方基準標註

**法庭可防禦性等級**：4/10 → 9/10（P0+P1+P2 已完成；剩 P2.5-C dashboard 與報告地圖截圖）。

---

## 二、技術棧

| 層 | 工具 |
|---|---|
| 後端 | FastAPI + uvicorn (port 8000) |
| DB | PostGIS 16 (Docker, port 5432) — `postgis/postgis:16-3.4` |
| Cache | Redis（**目前沒跑**，stats/hit 與 geocode cache 失效但不致命） |
| 前端 | 純 HTML/JS/Leaflet（無 build step），靜態檔案 port 5501 |
| Python | 3.13.2（homebrew），venv 在 `backend/.venv` |
| Repo | https://github.com/ChenJun43691/celltrail |

**本機路徑**：`~/Desktop/Python程序開發/CellTrail`（含中文路徑，命令注意 quote）。

**`.env` 重點**：`AUTH_ENABLED=false`（本機開發 anonymous admin），production 必須改 `true`。

---

## 三、常用命令

### 啟動基礎設施

```bash
# 啟動 PostGIS（Redis 選配）
docker compose -f infra/docker-compose.yml up -d db
# Redis 若需要：
docker compose -f infra/docker-compose.yml up -d redis

# 套用 DB schema（冪等，IF NOT EXISTS，重跑安全）
# 等 docker ps 顯示 (healthy) 再跑
docker exec -i celltrail_db psql -U celltrail -d celltrail < backend/app/db/schema.sql
# 或用腳本（含健康等待）：
bash backend/scripts/apply_schema_p0p1.sh
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
# http://127.0.0.1:5501  （5500 常被 VS Code Live Server 佔）
```

### 執行測試

```bash
cd backend
source .venv/bin/activate

# 全部測試
pytest app/tests/ -v

# 單一測試檔
pytest app/tests/test_ingest_match_col_idx.py -v

# 單一測試函式
pytest app/tests/test_audit.py::test_write_audit_fields -v
```

> smoke tests 不依賴 DB / Redis / Google，CI 可直接執行。

### 端對端 smoke test（需 DB + uvicorn 已啟）

```bash
bash backend/scripts/smoke_audit.sh
# 最後印綠色 ✓ 表示 audit chain 完整
```

### venv 救援（套件損壞時）

```bash
cd backend
bash scripts/rebuild_venv.sh   # 核彈級重建，約 5 分鐘
```

---

## 四、Milestone 進度（依 commit 時間軸）

### 已完成（git log 由舊到新）

| Commit | Milestone | 重點成果 |
|---|---|---|
| `9d62539` | **W2.1** Multi-sheet | sheet 跳過邏輯不再吃掉真資料 |
| `ea30d3a` | **W2.2** Buried header | 表頭被擠到第 N 行也找得到（前 25 行掃描評分） |
| `f548435` | **W2.3** Compound split | 「迄基地台」一欄含 cell_id+地址 → 拆解成兩欄；彭奕翔 0% → 100% |
| `54e5a25` | **W2.4** 中華上網方言 | 周蔓達 0% → 51.08%（物理上限）；電話通聯+歷程 4.97% → 60.31% 紅利 |
| `9a87c14` | **W2.5** _match_col_idx | 楊云豪 PDF 「細胞名稱」+「細胞」並存的 sector_id silent corruption；0/68 → 68/68 |
| `9bc0d42` | **W2.5-followup + P2.5-A** | popup 補 sector / azimuth_ref（紅字警告 unknown） |
| `37c93b8` | **P2.6** popup polish | azimuth=null 不再顯示「azimuth: °」空殼；cell_id 為空顯示「—」；azimuth=0 邊界 |
| `b62a64d` | **P2.5-B** azimuth-ref UI | 標註 modal（select 三選一 + evidence ≥5字 + by_ref 摘要）+ PATCH 流程 |

### 各真實樣本當前 normalize 通過率

| 樣本 | 通過率 | 備註 |
|---|---|---|
| 彭奕翔 `0801-0903...xlsx` | 100% | W2.3 後 |
| `網路歷程.xltx` | 100% | W1.5 baseline |
| `網路歷程-2a0c1c9a.xltx` | 100% | W1.5 baseline |
| `電話通聯+歷程.xlsx` | 60.31% | W2.4 紅利；剩餘 39.69% 為**物理失敗**（798 列無 cid + 362 列無 ts + 30 noise）|
| `周蔓達上網歷程.xlsx` | 51.08% | W2.4；達**資料物理上限**（47.35% 列原始就缺起台/起址） |
| `楊云豪黑莓卡852漫遊紀錄(含方位角)（已拖移） 2.pdf` | 100% + sector_id 100% 正確 | W2.5 |

**結論**：手邊所有真實樣本要嘛 100%、要嘛達資料物理上限。**ingest pipeline 沒有已知未修的 silent bug**。

---

## 五、關鍵設計決策（為什麼這樣做）

### A. 兩層方言系統（W2.4）

**問題**：中華上網方言中 `起台 → start_ts`、`起址 → cell_id`，但 W1 既有 `_RAW2CANON` 把 `起台 → cell_id`（錯）。直接修 W1 會壞掉 `_iter_rows_excel` 的 header detection scoring（用 `_RAW2CANON` 投票）。

**解法**：
- 全域 `_RAW2CANON` 保留為 **「header detection signal」**（決定哪行是 header，不負責正確性）
- 新增 `_DIALECT_HEADER_MAPS` 為 **「actual normalize rule」**（dialect-specific override，正確映射）
- `_iter_rows_excel` per-sheet 跑 `_detect_dialect`，命中後在每個 row 注入 reserved key `__celltrail_dialect__`
- `_normalize_row` 自動消化 tag → 走 dialect 路徑，對舊呼叫端**零異動**

### B. dialect 偵測雙訊號（W2.4）

避免誤判混合 sheet：必須**同時**滿足
1. headers 含 `{起台, 起址}` 雙指紋
2. ≥50% sample row 的「通話類別」含「上網」字串

設計上故意保守拒絕：sample 全空 → None；通話類別 < 50% 上網 → None。

### C. _match_col_idx 兩階段（W2.5）

**Bug**：PDF header 含「細胞名稱」+「細胞」並存時，`cands["cid"]=["細胞",...]` 用鬆散 `c in name` 比對，會在「細胞名稱」上提早命中 → sector / cid 撞到同一 index → silent corruption（sector_id 被填成 `'東港東方'` 這種地名）。

**解法**：兩階段
- **Pass 1**：精確匹配（canon equal），認領 index
- **Pass 2**：子字串備援，跳過 Pass 1 已認領的 index

**保留的容錯**：「基地臺編號」（臺）vs「基地台編號」（台）異體字、「連線開始時間」vs「開始時間」這種寬鬆欄名仍可在 Pass 2 命中。

### D. azimuth_ref 設計（P2.5）

**法庭背景**：電信業者 azimuth 的「北方基準」沒有統一規格（磁北/真北）。台灣高雄區磁偏角約 -4°~-5°，500m 距離下差出約 50m，足以差出整條街。法庭被詢問「此方位角的北方基準為何」答不出來，整套基地台扇形覆蓋推論的採信度會被質疑。

**設計**：
- DB 預設 `'unknown'`（不擅自推論）
- PATCH `/api/projects/{p}/targets/{t}/azimuth-ref` 由 admin 標註 `magnetic`/`true`/`unknown`
- evidence ≥5 字必填（書面依據描述）
- audit_logs 自動記錄誰、何時、依何書面證據

**P2.5-A**：popup 紅字「未標註 ⚠ 法庭風險」 — 設計用意是 UX 上發聲，逼使用者問「怎麼標」→ 觸發 P2.5-B 補完。

**P2.5-B**：series-line 加「方位角」link → modal（select + evidence + by_ref 分佈摘要）→ PATCH。

### E. popup 條件渲染（P2.6）

- `p.azimuth != null` 而非 truthy check（避免 azimuth=0「正北」被誤過濾）
- cell_id 為空顯示 「—」 而非空白
- azimuth_ref 行只在有 azimuth 時顯示（沒方位角 = 沒基準問題）

### F. _parse_ts ISO 8601 補強（W2.4-pre）

中華上網方言時間格式 100% 是 `2023-01-12T00:48:02.000`（T 分隔 + 毫秒）。新增兩個 strptime 格式：
- `%Y-%m-%dT%H:%M:%S.%f`
- `%Y-%m-%dT%H:%M:%S`

**故意拒絕** `Z` 與 `+08:00` 後綴：避免時區雙重標記歧義（系統假設都是 naïve 台北時間，由 _parse_ts 統一加 `TPE_TZ`）。

### G. 關鍵依賴版本約束

- **bcrypt 必須固定 `==4.2.1`**：passlib 1.7.4 與 bcrypt 5.x 不相容（"password cannot be longer than 72 bytes"）。等 passlib 1.8 釋出前不可升 bcrypt。
- **psycopg 所有 `cur.execute()` 必須帶 `prepare=False`**：connection pooler 不支援 server-side prepared statements，否則在池中換 connection 時會報 `prepared statement already exists`。
- **pytest 固定 `==8.3.3`**：pytest 9（大改版）與 anyio/fastapi 偶有相容性問題，無遷移迫切需求。

---

## 六、待辦事項（依優先級）

### 立即可做（≤30 分鐘）

| # | Task | 說明 | 時間 |
|---|---|---|---|
| 1 | **啟用 OSM 備援** | `.env` 加 `GEO_OSM_FALLBACK=1` + `NOMINATIM_EMAIL=...`；不需付費 | 5 分鐘 |
| 2 | **清 demo_case 舊資料** | 之前 4 次失敗上傳留下 geom NULL 記錄 + 同筆 evidence | 1 分鐘 |
| 3 | **重啟 uvicorn + 重新 ingest 楊云豪 PDF** | OSM 節流 ~70 秒 / 68 列 | 2 分鐘 |
| 4 | **跑 P2.5-B acceptance test A/B/C/D/E** | 點按鈕 → modal → 字數驗證 → 後端錯誤 → 成功 → popup 同步刷新 | 10 分鐘 |

### 中期（1-2 小時）

| # | Task | 說明 |
|---|---|---|
| 5 | **P2.5-C 法庭防禦性 dashboard** | unknown 比例 / 最近標註人 / audit_logs trail viewer / 報告 PDF 含基準 |
| 6 | **報告含地圖截圖** | reportlab 嵌 PNG，需 selenium/playwright 或 Leaflet 後端渲染 |
| 7 | **修 stats/hit CORS preflight** | 不影響功能但 console 一直噴錯 — preflight OPTIONS 沒被 CORSMiddleware 攔到 |

### 長期（半天以上）

| # | Task | 說明 |
|---|---|---|
| 8 | **多人多角色細緻權限** | 目前只 admin/user 兩級；檢警分艙、案件分艙 |
| 9 | **task #27 uvicorn `--reload` Python 3.13 macOS spawn bug** | 可能要改 watchmedo |
| 10 | **本地 cell_id → lat/lng 對照表** | 從業者拿基地台座標表，徹底解決 geocode 依賴 |

---

## 七、已知問題與環境陷阱

### 1. geocode 全敗會讓地圖空白

**症狀**：
- 上傳成功（uvicorn `200 OK`、evidence_id 有發）
- 但 `/map-layers` 回 0 點
- 前端 series 列表空 → P2.5-B 等 UI 入口無法觸發

**根因**：`/map-layers` 的 SQL 有 `WHERE geom IS NOT NULL`，geocode 失敗的列被過濾掉。

**確認**：
```sql
SELECT count(*) AS total, count(geom) AS with_geom
FROM raw_traces WHERE project_id=? AND deleted_at IS NULL;
```

**解法**：啟用 OSM 備援（待辦 #1）。

### 2. Redis 沒跑會噴 ConnectionRefused

`localhost:6379 connection refused`：影響 stats/hit dedup_key + geocode cache。**不致命**（geocode 邏輯失敗也回 None，不會 raise）。

要修：`docker compose -f infra/docker-compose.yml up -d redis`。

### 3. macOS Docker daemon 不會自動啟動

每次重開機要 `open -a Docker`，等 menu bar 鯨魚 icon 穩定再跑 `docker compose up -d db`。

### 4. apply_schema_p0p1.sh timing 陷阱

容器才剛 `health: starting` 就跑 schema 腳本會失敗（unix socket 還沒建好）。要先 `docker compose ps` 確認 `(healthy)` 再跑。

但 schema 是 idempotent（`IF NOT EXISTS`），重跑安全。

### 5. PDF ingest 慢（OSM 節流）

68 列楊云豪 PDF 啟用 OSM 後 ~70 秒。`上傳中…` toast 期間請耐心等，不要重複點。

### 6. 含中文路徑

repo 路徑 `~/Desktop/Python程序開發/CellTrail`、樣本檔名也含中文括號全形空格。shell 命令必須 quote，curl 命令的 `@filepath` 要用反斜線 escape。

---

## 八、git commit message 風格

依使用者偏好（精確、深入、邏輯嚴密、繁體中文）：

```
<milestone-id>: <短摘要英文>

Background:
- 用條列說明問題與根因（含「為什麼會發生」）
- 嚴格區分「事實／推論／結論」

Fix (簡述方案類型):
- 用條列描述每個修法決策的理由
- 「為什麼這樣做」優於「做了什麼」

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
4. 看 `backend/app/services/ingest.py` 的 `_DIALECT_HEADER_MAPS`（目前只有 cht_internet）— 新增方言會在這
5. 看 `backend/app/db/schema.sql` `raw_traces` 表結構
6. 看 `frontend/index.html` line 649-660 popup 結構（W2.5-followup + P2.5-A + P2.6 累積在這）
7. 看 `frontend/index.html` line ~810 `openAzRefModal` 函式（P2.5-B 全部邏輯）

關鍵檔案地圖：

```
backend/app/
  services/
    ingest.py        ← 大部分邏輯（_normalize_row, _match_col_idx, _detect_dialect, _parse_ts）
    geocode.py       ← Google + OSM 備援（OSM 預設關閉）
    audit.py         ← write_audit() helper
    evidence.py      ← SHA-256 落地
    carrier_profile.py
  api/
    map.py           ← /map-layers + /unlocated
    targets.py       ← DELETE 軟刪 + PATCH /azimuth-ref
    audit.py         ← /audit/logs
    upload.py
    report.py        ← evidence-report PDF
  db/
    schema.sql       ← 完整 schema（含 carrier_profiles 預設 jsonb 對照表）
    session.py       ← psycopg pool
  tests/             ← pytest（92 passed 含 W2.5）

frontend/
  index.html         ← 主頁（含 popup, openAzRefModal, refreshSeriesPanel）
  audit.html         ← P2 稽核檢視

infra/docker-compose.yml   ← db + redis + tileserver
```
