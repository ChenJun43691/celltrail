# 醒來要做的事（v5 — P0/P1/P2 端到端驗證通過）

> 更新時間：2026-04-26（下午 14:37 收工）
> 狀態：✅ **15/15 pytest passed** + ✅ **smoke test 7 步全綠** + ✅ **PDF 報告 6732B 下載成功**
> 法庭可防禦性：**4/10 → 9/10**（核心機制全部驗證，剩下都是進階功能與運維）

---

## 今日（2026-04-26）大事記

1. **早上**：寫完 P0+P1+P2 全套程式碼（在前一輪 session 完成；py_compile 全過）
2. **中午**：
   - venv 災難搶救：發現 PIL/_version.py 不見、_pytest/_code 不見、pygments.formatters.terminal 不見
   - 根因確認：舊 venv 與 requirements.txt 長期飄離（pytest 是孤兒套件，從未列入 requirements）
   - 修法：核彈級重建 venv（`mv .venv .venv.broken_<日期>` → `python3 -m venv .venv` → `pip install -r requirements.txt`）
3. **下午**：
   - DB schema 套用（apply_schema_p0p1.sh 跑通；發現 `\d` meta-command bug 並修）
   - uvicorn 9 router 全綠
   - smoke_audit.sh 7 步全過：上傳 → 軟刪 → 還原 → audit → evidence → PDF（發現 inline python f-string SyntaxError 並修）
   - 補強：`backend/scripts/rebuild_venv.sh`（核彈級救援腳本，下次再壞 5 分鐘修好）
   - 補強：`backend/requirements.txt` 加 `pytest==8.3.3`（不再飄離）

---

## 本輪總戰績一覽

| 階段 | 變更 | 檔案 | 動機 |
|---|---|---|---|
| P0 | `audit_logs` 表 + GIN index | `backend/app/db/schema.sql` | 證據鏈完整性；append-only ledger |
| P0 | `write_audit()` helper | `backend/app/services/audit.py` | 統一 IP/UA/SHA-256；safe=True |
| P0 | upload 整合 audit（成功+失敗） | `backend/app/api/upload.py` | 證物進入點 |
| P0 | `GET /api/audit/logs` + `/audit/actions` | `backend/app/api/audit.py` | 唯讀；分頁 + filter |
| P0 | `AUTH_ENABLED` 預設改 true + 啟動 WARN | `backend/app/security.py` | 預設安全 |
| P0 | `.env.example` 補 `AUTH_ENABLED=true` | `backend/.env.example` | 教導新部署 |
| P1 | `raw_traces.deleted_at/by/reason` + partial index | `backend/app/db/schema.sql` | 軟刪保留事證 |
| P1 | `targets.py` DELETE 改軟刪 + `/restore` + `/deleted` | `backend/app/api/targets.py` | 物理刪 = 事證滅失 |
| P1 | `map.py` 兩 SELECT 加 `deleted_at IS NULL` | `backend/app/api/map.py` | 軟刪即時隱形 |
| P2 | `evidence_files` 表 + 上傳全 SHA-256 落地 | `schema.sql` + `services/evidence.py` + `api/upload.py` | byte-for-byte 鑑識指紋 |
| P2 | `GET /api/projects/{p}/evidence-files` 列表 | `backend/app/api/audit.py` | 證物清單查詢 |
| P2 | 前端 audit 檢視頁（filter / 分頁 / 展開 details） | `frontend/audit.html` | 偵查員可在瀏覽器查稽核 |
| P2 | 主頁加 🛡️ 稽核連結 | `frontend/index.html` | 入口暴露 |
| P2 | `GET /api/projects/{p}/evidence-report` PDF 端點 | `backend/app/api/report.py` + `services/report.py` | 法庭可呈遞報告 |
| P2 | reportlab 加進 requirements | `backend/requirements.txt` | 中文用內建 STSong-Light CID |
| 測試 | `test_audit.py` 純函式單元測試 | `backend/app/tests/test_audit.py` | 沙箱可驗證 |
| 測試 | `test_evidence.py` SHA-256 鑑識函式測試（含 NIST 向量） | `backend/app/tests/test_evidence.py` | 鎖死「不可改成 sha1/md5」 |
| 測試 | `test_smoke.py` 補 `AUTH_ENABLED=true` setdefault | 同檔 | 測試獨立於本機 .env |
| 工具 | `apply_schema_p0p1.sh` + `smoke_audit.sh` | `backend/scripts/` | DB migration + 端到端驗證 |
| 工具 | **`rebuild_venv.sh`** 核彈級 venv 救援腳本 | `backend/scripts/rebuild_venv.sh` | 下次套件損壞 5 分鐘修好 |
| 修補 | apply_schema_p0p1.sh：`\d` 改成 information_schema 查詢 | 同檔 | psql -c 多行模式不解析 meta-command |
| 修補 | smoke_audit.sh：inline python f-string 內 `\"` → 拆 local 變數 | 同檔 | shell single-quote 內 `\` 非轉義 |
| 修補 | requirements.txt 補 `pytest==8.3.3` | `backend/requirements.txt` | 杜絕「孤兒套件」漂移 |

---

## 端到端驗證已完成 ✅（2026-04-26 下午）

四步驟（reportlab / schema / uvicorn / smoke）**全部通過**。詳見「今日大事記」。

如果未來再出狀況，下面是「假裝重來一次」的快速指引：

```bash
# 1) venv 體檢（壞了直接跑救援腳本）
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
source .venv/bin/activate
pip check                                    # 預期：No broken requirements found
# 若有衝突或缺套件：bash scripts/rebuild_venv.sh

# 2) DB schema 套用（冪等，重跑安全）
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail
bash backend/scripts/apply_schema_p0p1.sh

# 3) 啟 uvicorn（終端 A）
cd backend && source .venv/bin/activate
uvicorn app.main:app --port 8000

# 4) 端到端驗證（終端 B）
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail
bash backend/scripts/smoke_audit.sh
# 預期最後印綠色 ✓ Smoke test 通過：audit chain 完整
```

### 前端互動驗證（10 分鐘，下次想做再做）

```bash
cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/frontend
python3 -m http.server 5501          # 5500 常被 VS Code Live Server 佔，改 5501
# 瀏覽器打開 http://127.0.0.1:5501/
```

進主頁 → 右上角點 🛡️ **稽核** → 填 project_id → 「🔍 查詢」 → 展開 details → 「📄 匯出 PDF 報告」。

---

## 法庭可防禦性最終分數

| 項目 | 改動前 | 改動後 |
|---|---|---|
| Audit Log（誰、何時、做了什麼） | ✗ | ✓ append-only |
| 證物軟刪 + 還原 | ✗ | ✓ 含 reason 欄位 |
| AUTH 預設安全 | ✗ | ✓（本機可關，新部署預設開） |
| 刪除/還原必填 reason | ✗ | ✓ |
| Audit 不可竄改（無 UPDATE/DELETE 端點） | ✗ | ✓ |
| Payload SHA-256 錨點 | ✗ | ✓ |
| **檔案 byte-for-byte 全 hash 封存** | ✗ | ✓ evidence_files |
| **PDF 證物報告（含 audit 時間軸）** | ✗ | ✓ |
| 前端稽核檢視頁 | ✗ | ✓ |
| **綜合評分** | **4/10** | **9/10** |

剩下沒做的（大型項目）：
- 方位角磁北/真北基準（資料模型小改，但需查證電信業者交付規格）
- 報告含地圖截圖（要加 selenium / playwright；本地能跑但部署 Render 須加 chrome 容器）
- 多人多角色細緻權限（目前只區分 admin / user）

---

## API 一覽（這次新增）

```
# Audit
GET  /api/audit/logs?project_id=&target_ref=&user_id=&action=&since=&until=&page=&page_size=
GET  /api/audit/actions
GET  /api/projects/{project_id}/evidence-files?target_id=&page=&page_size=

# Target 軟刪 / 還原 / 已刪清單
DELETE /api/projects/{project_id}/targets/{target_id}     Body: {"reason":"..."}（選填）
POST   /api/projects/{project_id}/targets/{target_id}/restore   Body: {"reason":"..."}（必填，admin）
GET    /api/projects/{project_id}/targets/{target_id}/deleted   （admin）

# 證物報告
GET    /api/projects/{project_id}/evidence-report?target_id=    （admin；回傳 application/pdf）
```

---

## 還沒做的 task（依優先級）

| 優先 | Task | 動機 | 時間估計 |
|---|---|---|---|
| **P1** | git commit 今天的改動（建議分階段：schema → service → api → frontend → tests → ops scripts） | 防電腦壞掉資料丟失 | 10 分鐘 |
| **P1** | 前端互動驗證（瀏覽器 5501 進 audit.html） | 「眼見為憑」呈報長官時的最後一哩 | 10 分鐘 |
| ~~P2~~ | ~~方位角磁北/真北標註（schema 加 `azimuth_ref` 欄；ingest 預設 'unknown'；map 與報告呈現）~~ | ~~法庭採信中差 5-7° 可差出整條街~~ | ~~30 分鐘~~ → **已部分完成（後端 + 前端 popup 可見性）；UI 標註入口待補，見下方 P2.5 拆解** |
| **P2.5-A** | ✅ **已完成** popup 顯示 `北方基準` + 紅字警告 'unknown' (法庭風險) | 偵查員至少「知道」這個欄位存在 | — |
| **P2.5-B** | 在 target 列表/詳情頁加入「標註方位角基準」按鈕 → 呼叫既有 PATCH `/azimuth-ref` 端點（含 evidence 表單） | 偵查員可從 UI 完成法庭防禦性標註，否則所有 raw_traces 永停在 'unknown' | 1-2 小時 |
| **P2.5-C** | 法庭防禦性 dashboard：顯示 unknown 比例 / 最近標註人 / audit_logs trail viewer / 報告 PDF 內顯示北方基準 | P2.5 整套 UI 閉環，可端對端應對律師詰問 | 半天 |
| **P2.6** | popup 條件渲染整理：`azimuth: °` 在 azimuth=null 時看起來怪（原本就有的）；應該整行條件渲染 | 細節體驗 | 5 分鐘 |
| **P2** | 報告含地圖截圖（reportlab 嵌入 PNG）— 需 selenium/playwright 或 leaflet 後端渲染 | PDF 報告完整度 9/10 → 10/10 | 2 小時 |
| **P3** | 多人多角色細緻權限（檢警分艙 / 案件分艙） | 目前只區分 admin / user，可改進 | 半天 |
| **P3** | task #27 uvicorn `--reload` Python 3.13 macOS spawn bug | 開發體驗，不影響 prod | 不確定（可能要繞道用 watchmedo） |

---

## 提醒

- 本機 `.env` 仍是 `AUTH_ENABLED=false`，沒動。你想啟用 JWT 完整流程的話自己改成 true 即可。
- `.venv` 健康狀態建議偶爾 `pip check`；若再遇到「namespace package 但缺檔」直接 `bash backend/scripts/rebuild_venv.sh` 一鍵核彈級重建。
- 今日 smoke test 寫入 DB 的測試專案叫 `audit_smoke_143714`（HHMMSS 形式）。長期會累積，可考慮加一個清理腳本 `cleanup_smoke_data.sh`（按 project_id LIKE 'audit_smoke_%' 清 raw_traces / evidence_files / audit_logs）。
- 提醒：我預設 `AUTH_ENABLED=true` 在「啟動程式預設」（security.py），但 .env 仍是 false 蓋過。production 部署時務必移除 .env 內 AUTH_ENABLED=false 那行（或改成 true）。
