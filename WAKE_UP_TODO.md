# 醒來要做的事（v18）

> 更新時間：2026-08-22　｜　程式碼對齊 commit `4de8b38`，本地與 `origin/main` 同步
>
> **這份檔只回答「下一步該按什麼鍵」。**任何「為什麼這樣做」「這個決策的背景」
> 「哪個格式怎麼解」一律去查 `CLAUDE.md` —— 那才是唯一真實來源。
>
> v1–v17（2026-05-26 以前）的逐輪大事記已移除：內容停在三個月前、與現況嚴重
> 不符，留著只會誤導。需要回溯請用 `git log --oneline` 或 `git show <sha>:WAKE_UP_TODO.md`。

---

## 一句話現況

**解析已經很好，定位是 0。**

ingest pipeline 手邊 16 個真實樣本全數 100% 或達資料物理上限、無已知未修的 silent bug；
但線上 `cell_towers` 是空表、Google 停用、OSM 停用（會回錯座標，見 `CLAUDE.md` 七-11）
→ **上傳成功、解析成功、地圖沒有點**。所有其他待辦的實際價值都被這一件事壓著。

---

## 下一步（照順序）

### ① 把手上那 116 筆推估座標匯進線上，然後驗收 ← **投報率最高**

`data/` 底下有一份 `geocode_verify.py` 產出的已驗證推估座標 CSV（116 筆 / 14 個站點，
通過行政區＋路名雙重反查）。格式已檢查可直接匯入。

1. 先確認你接受它的限度：**這是地址推估值、不是業者座標，精度為「路名正確」而非
   「門牌正確」**，點會落在該路某處。每列 memo 都已標註來源，日後業者對照表到手
   直接重匯即覆蓋（`ON CONFLICT DO UPDATE`）。
2. `admin.html` → 基地台座標表 → 匯入 CSV（需 admin 登入；`/api/cell-towers/import`）。
3. 驗收（**不需要 token**，跑在本機就行）：

   ```bash
   cd backend
   python3 scripts/probe_cell_towers.py --expect ../data/<那份>.csv --sample 30
   ```

   它會抽樣打 production 的訪客端點，確認：**(a)** 這些 cell_id 真的能定位了、
   **(b)** 回傳座標與 CSV 相符 —— 後者是在抓「匯進去了但經緯度對調」這種
   在地圖上看起來完全正常的錯誤（`CLAUDE.md` 五-X）。

> 2026-08-22 實測基準：匯入前 **0/30 定位成功**。匯入後若沒變，先確認匯入回應的
> `inserted` 數字，再確認 cell_id 字面是否完全一致（前後空白、科學記號變形）。

### ② 待辦 #1 正題：向業者索取真正的基地台座標表

①只是過渡。手邊 16 檔共需 **6,620 個唯一 cell_id**、96.1% 的列帶 cell_id
→ 這條路一旦通，**定位率與速度問題同時消失**，且完全不依賴任何外部 geocoding 服務，
也是唯一撐得住法庭質詢的來源。索取時注意 `CLAUDE.md` 五-X 記的短碼唯一性問題
（中華上網方言的 3–5 碼編號可能需附 LAC/TAC 或原調閱案號才能定位到唯一站台）。

### ③ 待辦 #0：preview 路徑 payload 瘦身

`parse-only` / `parse-temp` 仍同時回 `_records` + GeoJSON（記憶體 ×2），大檔預覽仍可能 502。
正式 `/upload` 已由 P8.1 chunking 解決，**只剩預覽路徑**。方向：移除 `_records` 重複、
或改 `?include_records=1` 才回、或分頁／NDJSON。

### ④ 一個等你拍板的產品決策：`REPORT_ACL_SPEC_MISMATCH`

`api/report.py` 的 `evidence_report` docstring 寫「需 viewer 以上」，實作卻是 admin-only
（實測 project owner → 403、admin → 200）。**兩邊擇一改**：(a) 只有 admin 能出報告 → 改
docstring；(b) 應該 viewer+ → 改 guard 為 `assert_project_access(viewer)`。

---

## 千萬別做的兩件事

1. **不要把 OSM (`GEO_OSM_FALLBACK`) 開回去當定位主力。** 它對台灣地址會回傳
   「看起來完全正常但錯誤」的座標（實測偏差可達 26 公里、甚至比對到別的行政區的同名路）。
   要用必須加反查驗證層 —— 那正是 `geocode_verify.py` 在做的事，且它慢到只能離線跑。
   完整案例見 `CLAUDE.md` 七-11。
2. **不要匯入 `cell_towers_from_addr.csv` 那類未經反查驗證的推估座標。**
   同上，命中率數字漂亮但其中一半是錯的。

---

## 環境開機順序（本機）

```bash
open -a Docker                                              # 等鯨魚 icon 穩定
docker compose -f infra/docker-compose.yml up -d db         # 等 (healthy)
cd backend && source .venv/bin/activate && uvicorn app.main:app --port 8000 --reload
cd frontend && python3 -m http.server 5501                  # 另開一個終端
```

測試：`cd backend && pytest app/tests/ -v`（2026-08-22 實測 **503 passed**，約 13 秒，不需 DB/Redis/Google）

> 提醒：**使用者主要在雲端測**。本機改好不等於線上好了 —— 要 `git push origin main`
> 觸發 Render 自動 redeploy（約 2–5 分鐘）。

---

## 文件維護

- `AGENTS.md` **不要手改**：`bash scripts/sync_agents_md.sh` 由 `CLAUDE.md` 產生
  （`--check` 可驗證是否同步）。這兩份曾 drift 一個多月，導致 Codex 讀到的版本
  缺少七-11 的錯誤座標警告。
- 這份 `WAKE_UP_TODO.md` 一輪結束時更新；**細節寫進 `CLAUDE.md`，這裡只留動作**。
