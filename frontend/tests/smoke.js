// frontend/tests/smoke.js
// ---------------------------------------------------------------------------
// 前端 UI smoke test —— playwright-core 驅動系統 Chrome，跑 7 個頁面。
//
// 為什麼用 playwright-core 而非 playwright：core 不下載瀏覽器，改用系統
// Chrome（macOS 標配），repo 體積與 npm install 時間都比較友善。
//
// 前置：
//   - DB + uvicorn (port 8000) + 前端 http.server (port 5501) 已啟動
//   - 系統有 Chrome（macOS：/Applications/Google Chrome.app）
//   - （選配）CT_SMOKE_TOKEN=<admin token>
//        → 啟用 admin/audit 深度檢查；未設則只跑公開頁與守衛檢查
//
// 設計取捨：
//   - favicon.ico 404 是「專案無 favicon」的普遍現象，過濾不算 fail，
//     避免每個頁面都被誤判
//   - 不對 DB 做寫入（DELETE 案件、建分享連結等由 pytest 覆蓋），這支只
//     負責 UI 渲染與互動
//
// 退出碼：0 全綠 ｜ 1 任一檢查失敗 ｜ 2 執行錯誤（Chrome 未啟、5501 沒回應）
// ---------------------------------------------------------------------------
'use strict';

const { chromium } = require('playwright-core');

const FE_BASE = process.env.CT_FE_BASE || 'http://127.0.0.1:5501';
const TOKEN   = process.env.CT_SMOKE_TOKEN || null;

const PASSES = [];
const FAILS  = [];
function assert(name, cond, info = '') {
  if (cond) PASSES.push(name);
  else FAILS.push({ name, info: String(info).slice(0, 200) });
}

// 兩種「不算 fail」的雜訊：
//   1. favicon.ico 404 —— 專案無 favicon，每頁瀏覽器都會自動索取一次
//   2. /api/auth/me 401 —— index.html 為了支援匿名訪客（P5 parse-only），
//      載入時會打這支試探登入狀態；沒 token 拿 401 是「未登入」訊號，
//      頁面也正確當「訪客模式」處理（不會拋例外、不影響功能）
// 兩者都同時會在 console.error 與 response 兩處出現，兩處都過濾才不會誤判。
function filterNoise(consoleMsgs, badResponses) {
  const consoleClean = consoleMsgs.filter(m =>
    !/favicon\.ico/i.test(m) &&
    !/Failed to load resource.*404/i.test(m) &&
    !/Failed to load resource.*401/i.test(m));
  const respClean = badResponses.filter(r =>
    !/\/favicon\.ico(\?|$)/i.test(r) &&
    !/\/api\/auth\/me(\?|$)/i.test(r));
  return { consoleClean, respClean };
}

async function openPage(ctx, url, { token } = {}) {
  const page = await ctx.newPage();
  const consoleMsgs = [], pageErrors = [], badResponses = [];
  page.on('console',   m => { if (m.type() === 'error') consoleMsgs.push(m.text()); });
  page.on('pageerror', e => pageErrors.push(e.message));
  page.on('response',  r => { if (r.status() >= 400) badResponses.push(`${r.status()} ${r.url()}`); });
  if (token) await page.addInitScript(t => localStorage.setItem('ct_token', t), token);
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 });
  return { page, consoleMsgs, pageErrors, badResponses };
}

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });

  // ── A. 公開頁面（無需登入即應可開）─────────────────────────────
  // 檢查重點：window.CT_API 設妥（api.js 載入順序正確）、無 JS 例外、
  //          無非預期 4xx（過濾 favicon）、無非預期 console error。
  for (const name of ['login', 'register', 'index']) {
    const { page, consoleMsgs, pageErrors, badResponses } =
      await openPage(ctx, `${FE_BASE}/${name}.html`);
    await page.waitForTimeout(1500);
    const api = await page.evaluate(() => window.CT_API).catch(() => null);
    const { consoleClean, respClean } = filterNoise(consoleMsgs, badResponses);
    assert(`${name}.html: window.CT_API 已設`,
      typeof api === 'string' && api.endsWith('/api'), api || '(null)');
    assert(`${name}.html: 無 pageerror`,
      pageErrors.length === 0, pageErrors.join(' | '));
    assert(`${name}.html: 無非預期 4xx`,
      respClean.length === 0, respClean.join(' | '));
    assert(`${name}.html: 無非預期 console error`,
      consoleClean.length === 0, consoleClean.join(' | '));
    await page.close();
  }

  // ── B. share.html 無 token：應顯示錯誤畫面、不可拋例外 ─────────
  {
    const { page, pageErrors } = await openPage(ctx, `${FE_BASE}/share.html`);
    await page.waitForTimeout(1500);
    assert('share.html 無 token: 無 pageerror',
      pageErrors.length === 0, pageErrors.join(' | '));
    await page.close();
  }

  // ── C. 守衛重導向：admin/audit 無 token 應導回 login ───────────
  // 為什麼這個測試重要：之前 logout 後 admin.html 卡在白屏的 regression
  // 出過一次；自動驗證導向避免重蹈覆轍。
  for (const name of ['admin', 'audit']) {
    const { page, pageErrors } = await openPage(ctx, `${FE_BASE}/${name}.html`);
    await page.waitForTimeout(2500);
    const final = page.url();
    assert(`${name}.html 無 token: 導回 login.html`,
      final.includes('login.html'), final);
    assert(`${name}.html 無 token: 無 pageerror`,
      pageErrors.length === 0, pageErrors.join(' | '));
    await page.close();
  }

  // ── D. 深度檢查（需 CT_SMOKE_TOKEN）────────────────────────────
  // 不帶 token 也能跑 A/B/C；帶 token 才能驗 admin 三分頁與 audit 查詢。
  if (TOKEN) {
    // admin.html：分頁切換 + 各分頁資料載入
    const { page, consoleMsgs, pageErrors, badResponses } =
      await openPage(ctx, `${FE_BASE}/admin.html`, { token: TOKEN });
    await page.waitForTimeout(3000); // init() 打 /auth/me + 預設分頁載入
    assert('admin.html 帶 token: 未導回 login',
      !page.url().includes('login.html'), page.url());
    assert('admin.html: 渲染 3 個 tab-btn',
      (await page.locator('.tab-btn').count()) === 3);

    // 切「資料表」分頁
    await page.locator('.tab-btn[data-tab="data"]').click();
    await page.waitForTimeout(2000);
    assert('admin.html: 點「資料表」分頁切換成功',
      (await page.locator('.tab-panel[data-tab-panel="data"].active').count()) === 1);
    const towerStats = ((await page.locator('#towerStats').textContent()) || '').trim();
    assert('admin.html: towerStats 完成載入（非「載入中…」）',
      towerStats.length > 0 && !towerStats.includes('載入中'),
      towerStats.slice(0, 50));

    // 切「稽核」分頁
    await page.locator('.tab-btn[data-tab="audit"]').click();
    await page.waitForTimeout(2000);
    assert('admin.html: 點「稽核」分頁切換成功',
      (await page.locator('.tab-panel[data-tab-panel="audit"].active').count()) === 1);
    assert('admin.html: auditTbody 有實際資料列',
      (await page.locator('#auditTbody tr').count()) >= 1);

    const { consoleClean, respClean } = filterNoise(consoleMsgs, badResponses);
    assert('admin.html: 無 pageerror',
      pageErrors.length === 0, pageErrors.join(' | '));
    assert('admin.html: 無非預期 4xx',
      respClean.length === 0, respClean.join(' | '));
    assert('admin.html: 無非預期 console error',
      consoleClean.length === 0, consoleClean.join(' | '));
    await page.close();

    // audit.html：點「查詢」應載入紀錄
    {
      const { page, pageErrors } = await openPage(ctx, `${FE_BASE}/audit.html`, { token: TOKEN });
      await page.waitForTimeout(1500);
      await page.locator('#btnQuery').click();
      await page.waitForTimeout(3000);
      const rows = await page.locator('#tbody tr.row-main').count();
      assert('audit.html 帶 token: 點「查詢」載入 ≥1 列',
        rows >= 1, `rows=${rows}`);
      assert('audit.html 帶 token: 無 pageerror',
        pageErrors.length === 0, pageErrors.join(' | '));
      await page.close();
    }

    // index.html 帶 token：跳過「請選擇使用模式」modal，自動進「建立新專案」（commit a713d38）
    // 為什麼測這個：commit a713d38 移除登入後的模式選擇 modal、改成自動進 project modal。
    // 若未來有人不小心把舊邏輯加回來，會直接破壞登入者預期。
    {
      const { page, pageErrors } = await openPage(ctx, `${FE_BASE}/index.html`, { token: TOKEN });
      // 確保不被「記住 = 臨時」的 SESSION_MODE_KEY 干擾（測「預設行為」）
      await page.evaluate(() => localStorage.removeItem('ct_session_mode'));
      await page.reload({ waitUntil: 'domcontentloaded' });
      // initAuth → /auth/me → initAfterLogin → openProjectCreationModal
      await page.waitForTimeout(3500);

      const hasModeSelect = await page.locator('text=請選擇使用模式').count();
      assert('index.html 帶 token: 不彈出「請選擇使用模式」modal',
        hasModeSelect === 0, `count=${hasModeSelect}`);

      // 用帶 emoji 的「📁 建立新專案」精確比對 modal 標題（dropdown 的版本是「＋ 建立新專案」）
      const hasProjModal = await page.locator('text=📁 建立新專案').count();
      assert('index.html 帶 token: 自動進入「📁 建立新專案」modal',
        hasProjModal >= 1, `count=${hasProjModal}`);

      // 進階設定 summary 預設文字（無資料時不該已掛 ✦；commit 7d657aa）
      const sumText = ((await page.locator('#advancedSection > summary').textContent()) || '').trim();
      assert('index.html: 進階設定 summary 預設不含 ✦（無資料時）',
        sumText.length > 0 && !sumText.includes('✦'), sumText);

      assert('index.html 帶 token: 無 pageerror',
        pageErrors.length === 0, pageErrors.join(' | '));
      await page.close();
    }
  } else {
    console.log('ℹ️  CT_SMOKE_TOKEN 未設 — 跳過 admin/audit 深度檢查');
    console.log('   要啟用：export CT_SMOKE_TOKEN=$(bash mint-token.sh <admin-username>)');
  }

  await browser.close();

  // ── 輸出結果 ──────────────────────────────────────────────────
  console.log(`\n通過 ${PASSES.length} ｜ 失敗 ${FAILS.length}`);
  for (const p of PASSES) console.log('  ✓ ' + p);
  if (FAILS.length) {
    console.log('\n失敗詳情：');
    for (const f of FAILS) console.log(`  ✗ ${f.name}${f.info ? '  ── ' + f.info : ''}`);
    process.exit(1);
  }
})().catch(e => {
  console.error('FATAL', e.message);
  console.error('  常見原因：DB/uvicorn/前端 5501 未啟動，或系統無 Chrome');
  process.exit(2);
});
