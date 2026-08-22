# 醒來要做的事（v21）

> 更新時間：2026-08-22（全樣本盤點 + TGOS/NLSC 定位路徑）　｜　程式碼對齊 commit `b49d415`
>
> **這份檔只回答「下一步該按什麼鍵」。**任何「為什麼這樣做」「這個決策的背景」
> 「哪個格式怎麼解」一律去查 `CLAUDE.md` —— 那才是唯一真實來源。
>
> v1–v17（2026-05-26 以前）的逐輪大事記已移除：內容停在三個月前、與現況嚴重
> 不符，留著只會誤導。需要回溯請用 `git log --oneline` 或 `git show <sha>:WAKE_UP_TODO.md`。

---

## 一句話現況

**Excel 的辨識已經沒問題了；卡住的是「地址 → 座標」這一步，而它需要你去申請一組帳號。**

全 34 檔盤點（`CLAUDE.md` 七-13）：Excel/CSV 全部解析成功、共 119,764 列，逐列都帶基地台
編號與地址；**但只有 19.3% 定位得到座標**，而且那全部來自那份 116 筆推估表。
13 個解析失敗的全是 PDF（其中 7 個有同案 xlsx 可替代，你已說先別管 PDF）。

關鍵數字：這十二萬列其實只對應 **3,459 個不重複地址**（前 800 個就涵蓋 95% 的列）。
所以要「全部定位」不是查十二萬次，是把三千多個地址查一次、寫進 `cell_towers`。

現在的問題是**沒有任何一條正向地理編碼路徑可用**（`CLAUDE.md` 七-14）：
Google 金鑰無效、OSM 停用（會回錯座標）、TGOS 沒憑證。

---

## 下一步（照順序）

### ① 申請 TGOS 全國門牌地址定位服務 ← **現在最重要的一步，只有你能做**

這是「全部定位」唯一實際可行的路。內政部的服務，整合**戶政機關門牌坐標**，
對政府機關**免費**，你的 kcg.gov.tw 身分符合資格。

1. https://www.tgos.tw → 註冊會員 → 申請「全國門牌地址定位服務」
   （客服 tgos@moi.gov.tw、02-21927111）
2. 拿到 AppID / APIKey 後，跑一次離線批次：

   ```bash
   cd backend && source .venv/bin/activate
   export TGOS_APP_ID=你的AppID TGOS_API_KEY=你的APIKey
   python scripts/geocode_verify.py ../基地台位置範例檔案 \
       -o ../data/towers_tgos.csv --provider tgos --verifier nlsc
   ```
   每個結果都會用**內政部 NLSC 官方行政區**反查驗證，跨行政區的錯誤匹配會被擋下。
3. `admin.html` → 基地台座標表 → 匯入產出的 CSV。

程式端已經寫好等你貼金鑰（`CLAUDE.md` 七-14）。**但它沒對真實服務測過**（我沒有憑證），
第一次跑若回應格式對不上，把錯誤訊息貼給我調 `_tgos_parse`。

**上限說明**：門牌型地址佔 95.0% 的列，可望全解。剩下的是「地號」（需地籍資料，約 5%）、
「電桿」，以及 480 列的 `上網或使用VoWiFi` / `(簡訊系統發信)` —— 最後這類**本來就不是位置**，
定位不到是正確行為。所以實際上限是 **99.6%**，不是 100%。

### ①-b 那份 116 筆推估座標：可以先匯，已雙重驗證

`data/cell_towers_橋檢_top40_已驗證.csv`。原本只有 OSM 反查背書，
**2026-08-22 已用內政部 NLSC 官方行政區資料重驗：14 個站點全數相符、116/116 通過**。
另已在本機真 DB 走正式 API 匯入過（`inserted=116`、座標未經緯度對調）。

`admin.html` → 基地台座標表 → 匯入 CSV，然後驗收（不需 token）：

```bash
cd backend
python3 scripts/probe_cell_towers.py --expect "../data/cell_towers_橋檢_top40_已驗證.csv" --sample 30
```

> 匯入前基準：0/30 定位成功。它能把「蘇」系列從 0 推到約四成，但「陳」系列只有 3–5%
> —— 所以這只是過渡，真正的解法是 ① 或 ②。

### ② 待辦 #1 正題：向業者索取真正的基地台座標表

①只是過渡。手邊 16 檔共需 **6,620 個唯一 cell_id**、96.1% 的列帶 cell_id
→ 這條路一旦通，**定位率與速度問題同時消失**，且完全不依賴任何外部 geocoding 服務，
也是唯一撐得住法庭質詢的來源。索取時注意 `CLAUDE.md` 五-X 記的短碼唯一性問題
（中華上網方言的 3–5 碼編號可能需附 LAC/TAC 或原調閱案號才能定位到唯一站台）。

### ③ P9 Phase 2B —— ✅ 已完成、已部署、已線上驗收（不用做事）

待辦 #0 的主體已結案：訪客與手動欄位對應都切到 Preview Artifact，前端三條上傳路徑
都不再取得 `_records`，實測同檔 payload 縮減 **67–94%**。

已直接打 production 驗過 12 項（訪客建立/讀取/撤銷、seal→401、mapping-aware 全鏈、
壞 mapping→400、靜態站已是新版），詳見 `CLAUDE.md`「P9 Phase 2B → 正式環境驗收」。
`PREVIEW_ARTIFACT_KEY` 確認仍在 Render（訪客 create 回 200 即為反證）。

**只有一段 production 還沒被實際走過**（需要你的帳號，我方無憑證）：

> 訪客上傳 → 按「登入並儲存」→ 登入回來 → 資料應該還在 → 儲存為專案應該成功。
> 這條路本機 DB-backed E2E 已完整覆蓋，但線上沒走過。你下次登入時順手試一次即可。
> 若資料沒還原：多半是超過 30 分鐘 TTL（畫面會明講「訪客預覽已失效」），重新上傳即可。

另外值得順手看一眼：上傳一個系統不認得的格式 → 手動對應 → 儲存為專案 →
該筆現在應該**有** evidence（這是 Phase 2B 補上的證據鏈缺口，以前這條路沒有）。

### ④ 其餘 P9 待辦（尚未動）

object storage A.5（5–50MB）、supervisor seal / custody ledger、
`geocoded_cell_estimates` 推估座標分表、report ACL 決策（REPORT_ACL_SPEC_MISMATCH）、
legacy `parse-only` / `parse-temp` / `save-records` 的實際移除（等舊快取頁面汰換）。
