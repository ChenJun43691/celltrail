# 醒來要做的事（v19）

> 更新時間：2026-08-22（P9 Phase 2B 後）　｜　本地已有未推送的 Phase 2B commit
>
> **這份檔只回答「下一步該按什麼鍵」。**任何「為什麼這樣做」「這個決策的背景」
> 「哪個格式怎麼解」一律去查 `CLAUDE.md` —— 那才是唯一真實來源。
>
> v1–v17（2026-05-26 以前）的逐輪大事記已移除：內容停在三個月前、與現況嚴重
> 不符，留著只會誤導。需要回溯請用 `git log --oneline` 或 `git show <sha>:WAKE_UP_TODO.md`。

---

## 一句話現況

**解析已經很好，定位是 0。**（Phase 2B 之後，「傳輸與證據鏈」也已經很好；仍然沒有點。）

ingest pipeline 手邊 16 個真實樣本全數 100% 或達資料物理上限、無已知未修的 silent bug；
但線上 `cell_towers` 是空表、Google 停用、OSM 停用（會回錯座標，見 `CLAUDE.md` 七-11）
→ **上傳成功、解析成功、地圖沒有點**。所有其他待辦的實際價值都被這一件事壓著。

---

## 下一步（照順序）

### ① 把手上那 116 筆推估座標匯進線上，然後驗收 ← **投報率最高、且只剩你能做的一步**

`data/cell_towers_橋檢_top40_已驗證.csv`。**匯入前的所有能自動化的驗證都已做完**
（2026-08-22，詳見 `CLAUDE.md` 七-12）：

- 用真實匯入邏輯離線預檢：**116 可匯入 / 0 跳過 / 0 錯誤**，欄序推導正確（未觸發五-X）。
- 唯一的離群站點（台中，距其餘 165 km）已回原始檔查證為**真實移動軌跡**，非地理編碼假影。
- 本機真 DB 走正式 API 匯入過一次：`inserted=116`，回查座標**未經緯度對調**。
- 本機實測效果：`028351` 定位 **0 → 3,758（40.5%）**、`031543` **0 → 1,996（41.6%）**；
  但 `026965` 只有 5.2%、`026962` 3.0%、`複本 031542` **0%**。

先確認你接受它的限度：**這是地址推估值、不是業者座標，精度為「路名正確」而非「門牌正確」**。
每列 memo 都已標註來源；日後業者對照表到手直接重匯即覆蓋（`ON CONFLICT DO UPDATE`）。

1. `admin.html` → 基地台座標表 → 匯入 CSV（需 admin 登入；`/api/admin/cell-towers/import`）。
2. 驗收（**不需要 token**，跑在本機就行）：

   ```bash
   cd backend
   python3 scripts/probe_cell_towers.py --expect "../data/cell_towers_橋檢_top40_已驗證.csv" --sample 30
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

### ③ 部署 P9 Phase 2B（本地已完成，等你 push）

待辦 #0 的主體已完成：訪客與手動欄位對應都切到 Preview Artifact，前端三條上傳路徑
都不再取得 `_records`。實測同檔 payload 縮減 **67–94%**。

```bash
git push origin main      # Render 自動 redeploy，約 2–5 分鐘
```

**部署前確認 Render 的 `PREVIEW_ARTIFACT_KEY` 還在**（`openssl rand -hex 32` 產生的 64 字元 hex）。
依 `CLAUDE.md` A.6 的紀錄，P9A 正式環境 smoke 曾實測 `POST /api/preview` 回 200 —— 那證明
當時已設好。但它是 fail-closed 的：一旦缺失，preview 一律回 503，而**訪客上傳現在走的就是
preview**，等於訪客路徑整條不能用（以前只影響登入版臨時查看）。這條路的風險等級因本輪而上升，
所以值得在部署前看一眼，而不是假設它還在。

部署後值得手動確認的三件事（都不需要特別工具）：
1. 未登入開 `index.html` 上傳一個歷程檔 → 應照常出點、Network 面板應看到 `POST /api/preview`。
2. 上傳一個系統不認得的格式 → 手動對應 → 儲存為專案 → 該筆應**有** evidence
   （這是 Phase 2B 補上的證據鏈缺口，以前這條路沒有）。
3. 訪客上傳 → 按「登入並儲存」→ 登入回來 → 資料應還在（現在是憑 preview_id 向 server 重取）。

### ④ 其餘 P9 待辦（尚未動）

object storage A.5（5–50MB）、supervisor seal / custody ledger、
`geocoded_cell_estimates` 推估座標分表、report ACL 決策（REPORT_ACL_SPEC_MISMATCH）、
legacy `parse-only` / `parse-temp` / `save-records` 的實際移除（等舊快取頁面汰換）。
