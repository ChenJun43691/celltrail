// frontend/api.js
// ---------------------------------------------------------
// 共用 API base —— 單一來源（single source of truth）。
//
// 各頁面以 <script src="./api.js"></script> 載入（classic script，
// 非 ES module），載入後即可使用兩個全域字串：
//   window.CT_API_BASE  例：http://localhost:8000      （不含 /api）
//   window.CT_API       例：http://localhost:8000/api  （含 /api）
//
// 解析優先序：
//   1. window.__CELLTRAIL_API__（測試 / staging 可在頁面先行覆寫）
//   2. 本機 hostname（localhost / 127.0.0.1 / 區網）→ http://localhost:8000
//   3. 其他 → 正式環境 Render 後端 URL
//
// 為什麼刻意維持 classic script（無 export）：
//   讓 module 與非 module 頁面都能以 <script src> 載入。module 頁面
//   只要在自己的 <script type="module"> 之前放這支 classic script，
//   即可讀到上述全域（classic script 先於 deferred module 執行）。
//
// 正式後端網址若變更，只改這一支檔案即可（原本散在 6 個 HTML）。
// ---------------------------------------------------------
(function () {
  function resolveApiBase() {
    if (window.__CELLTRAIL_API__) return window.__CELLTRAIL_API__.replace(/\/$/, '');
    const host = (typeof location !== 'undefined' && location.hostname) || '';
    const isLocal = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|192\.168\.|10\.)/.test(host);
    if (isLocal) return 'http://localhost:8000';
    return 'https://celltrail-api.onrender.com';
  }
  const base = resolveApiBase();
  window.CT_API_BASE = base;          // 不含 /api
  window.CT_API      = base + '/api'; // 含 /api
})();
