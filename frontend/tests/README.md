# 前端 UI smoke test

`playwright-core` 驅動系統 Chrome 跑 7 個前端頁面，檢查：

- **公開頁面**（`login` / `register` / `index`）：`window.CT_API` 設妥、無
  JS 例外、無非預期 4xx、無非預期 console error
- **`share.html` 無 token**：顯示錯誤畫面、不可拋例外
- **守衛重導向**（`admin` / `audit`）：無 token 應導回 `login.html`
- **（選配）深度檢查**（`CT_SMOKE_TOKEN` 已設時）：
  - `admin.html` 三分頁切換 + 各分頁資料載入
  - `audit.html` 點「查詢」載入 ≥1 列

不對 DB 做寫入 —— DB 行為由 `backend/app/tests/` 的 pytest 覆蓋。

---

## 一次性設定

```bash
cd frontend/tests
npm install      # 安裝 playwright-core（不下載瀏覽器，改驅動系統 Chrome）
```

需求：
- 系統有 Chrome（macOS 標配：`/Applications/Google Chrome.app/`）
- DB + uvicorn (port 8000) + 前端 http.server (port 5501) 已啟動
  （見 `CLAUDE.md` 第三節）

---

## 執行

**最小**（不需登入；跑 A/B/C 段）：

```bash
npm test
```

**深度**（含 admin 三分頁、audit 查詢；需 admin token）：

```bash
export CT_SMOKE_TOKEN=$(bash mint-token.sh CIDadmin)
npm test
```

`mint-token.sh` 用後端的 `SECRET_KEY` 直接鑄一個 token，不需密碼也不寫 DB；
僅 dev 環境使用。要換成其他 admin 帳號，把 `CIDadmin` 換成該帳號的 username。

---

## 退出碼

| Code | 意義 |
|---|---|
| 0 | 全部通過 |
| 1 | 任一檢查失敗（詳列於輸出末段） |
| 2 | 執行錯誤（Chrome 未啟、5501/8000 沒回應、venv 不在等） |

可接 CI（GitHub Actions 等）以 `npm test` 直接觸發。

---

## 設計取捨

- **為什麼用 `playwright-core` 而非 `playwright`**：core 不下載瀏覽器，改
  用系統 Chrome；repo 體積與 `npm install` 時間都比較友善。
- **為什麼過濾 `favicon.ico` 404**：專案無 favicon，每頁都會被瀏覽器自動
  索取一次，過濾才不會誤判每個頁面都有 4xx。
- **為什麼測 `admin/audit` 守衛重導向**：曾出過 logout 後 admin 卡白屏的
  regression，自動驗證導向避免重蹈覆轍。
- **為什麼不在這支測 DB 寫入**（建立分享連結 / 軟刪案件 …）：smoke 應
  快、且不該污染 DB；那些路徑由後端 pytest 覆蓋。
