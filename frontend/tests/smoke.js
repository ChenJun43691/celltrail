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

  // ── E. 地圖互動深度檢查（guest parse-only，不寫 DB）─────────────
  // 為什麼用 guest 模式：parse-only API 只解析不落 DB，既符合「smoke 不寫 DB」
  // 原則，又能把真實點位渲染到地圖上，驗證 上傳→解析→render→popup 管線
  // 與測距工具 —— 這些是 P6 後幾乎重寫、先前只靠人工測的互動。
  // 不需 token（訪客即走 parse-only），故 CI 無 token 也能跑到。
  {
    const path = require('path');
    const SAMPLE = path.resolve(__dirname, '..', '..', '基地台位置範例檔案', '網路歷程.xlsx');
    const { page, consoleMsgs, pageErrors, badResponses } =
      await openPage(ctx, `${FE_BASE}/index.html`);
    // 跳過新手導覽 overlay（會蓋住互動），再重載讓設定生效
    await page.evaluate(() => localStorage.setItem('ct_tour_done_v1', '1'));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    await page.keyboard.press('Escape'); // 收掉任何殘留 overlay（計數器等）

    // E1. 訪客/登入按鈕群組可見性（直接讀 computed display，不受側選單收合影響）
    //     —— 分享連結 / 授權成員鈕都在 #loggedInBtns 內，訪客模式必須整組隱藏。
    const grp = await page.evaluate(() => ({
      loggedIn: getComputedStyle(document.getElementById('loggedInBtns')).display,
      guest:    getComputedStyle(document.getElementById('guestBtns')).display,
    }));
    assert('index(guest): loggedInBtns（含分享/成員鈕）整組隱藏',
      grp.loggedIn === 'none', grp.loggedIn);
    assert('index(guest): guestBtns（登入/申請）顯示',
      grp.guest !== 'none', grp.guest);

    // E2. 測距工具：點 📏 → 結果框顯示；取消 → 隱藏
    await page.locator('#measureBtn').click();
    await page.waitForTimeout(400);
    assert('index(guest): 測距工具開啟 → 結果框顯示',
      await page.locator('#measurePanel').isVisible());
    await page.locator('#measureCancel').click();
    await page.waitForTimeout(300);
    assert('index(guest): 取消測距 → 結果框隱藏',
      !(await page.locator('#measurePanel').isVisible()));

    // E3. parse-only 上傳 → 地圖渲染 marker（#upl 為 hidden input，setInputFiles 直接設）
    await page.setInputFiles('#upl', SAMPLE);
    await page.waitForTimeout(400);
    await page.locator('#btnUpload').click();
    // 輪詢等 marker 出現（parse-only + geocode + render；最多 ~18s）
    let markerCount = 0;
    for (let i = 0; i < 36; i++) {
      markerCount = await page.locator('.leaflet-marker-icon').count();
      if (markerCount > 0) break;
      await page.waitForTimeout(500);
    }
    assert('index(guest): parse-only 上傳後地圖渲染 marker',
      markerCount > 0, `markers=${markerCount}`);

    // E4. 驗證 marker 綁定的 popup 內容正確渲染（含真實資料欄位）。
    //     直接讀 marker.getPopup().getContent()（透過 window.__ctTest seam）—— 這
    //     驗的是 popup 模板有正確帶入資料，比「在地圖上開 popup」更貼近本質且
    //     不受 markercluster 去叢集 / spiderfy 動畫時機影響（100% 確定性）。
    const popupHtml = await page.evaluate(() => {
      const t = window.__ctTest;
      if (!t || !t.seriesMap) return null;
      for (const s of t.seriesMap.values()) {
        const cl = s.layers && s.layers.cluster;
        const layers = (cl && cl.getLayers) ? cl.getLayers() : [];
        for (const m of layers) {
          const pu = m.getPopup && m.getPopup();
          if (pu) {
            const c = pu.getContent();
            return typeof c === 'string' ? c : (c && c.outerHTML) || '';
          }
        }
      }
      return '';
    });
    assert('index(guest): marker 綁定 popup 內容含預期欄位（cell_id / 精度）',
      popupHtml != null && /cell_id/.test(popupHtml) && /精度/.test(popupHtml),
      String(popupHtml).slice(0, 80));

    // E5. 整段互動流程無 JS 例外
    const { consoleClean } = filterNoise(consoleMsgs, badResponses);
    assert('index(guest) 地圖互動: 無 pageerror',
      pageErrors.length === 0, pageErrors.join(' | '));
    assert('index(guest) 地圖互動: 無非預期 console error',
      consoleClean.length === 0, consoleClean.join(' | '));
    await page.close();
  }

  // ── F. 加密 / 密碼保護檔上傳 → 跳出清楚錯誤提醒（不寫 DB）─────────
  // 合成一個 OLE2/CDFV2 檔頭（密碼保護 xlsx 的容器 magic）的假檔，上傳後
  // 後端應回 422 + 清楚訊息，前端以 toast 提示「請移除密碼」。
  {
    const { page, pageErrors } = await openPage(ctx, `${FE_BASE}/index.html`);
    await page.evaluate(() => localStorage.setItem('ct_tour_done_v1', '1'));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1200);
    await page.keyboard.press('Escape');

    const OLE2 = Buffer.concat([
      Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]),
      Buffer.alloc(600),
    ]);
    await page.setInputFiles('#upl', {
      name: '密碼保護.xlsx',
      mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      buffer: OLE2,
    });
    await page.waitForTimeout(300);
    await page.locator('#btnUpload').click();
    // 等錯誤 toast 出現（toast 文字即訊息；error toast 預設顯示 6s）
    let toastText = '';
    for (let i = 0; i < 16; i++) {
      toastText = ((await page.locator('.toast').textContent().catch(() => '')) || '');
      if (/密碼|加密/.test(toastText)) break;
      await page.waitForTimeout(400);
    }
    assert('index(guest): 加密檔上傳 → 跳出「密碼/加密」錯誤提醒',
      /密碼|加密/.test(toastText), `toast=${toastText.slice(0, 50)}`);
    assert('index(guest): 加密檔上傳無 pageerror',
      pageErrors.length === 0, pageErrors.join(' | '));
    await page.close();
  }

  // ── G. 怪欄名 → 問答式手動對應（「時間在哪一欄？」+ 依範例值自動猜）─────
  // 欄名各家業者用語不一，解不出來時不該逼使用者看懂欄名 —— 改問「哪一欄是
  // 時間/地點」並秀範例值。本測試：上傳系統認不得欄名的 CSV → 診斷 modal →
  // 點手動對應 → 驗證問答式 modal 出現、且依範例值自動猜對時間欄。
  {
    const { page, pageErrors } = await openPage(ctx, `${FE_BASE}/index.html`);
    await page.evaluate(() => localStorage.setItem('ct_tour_done_v1', '1'));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1200);
    await page.keyboard.press('Escape');

    const csv =
      '代號,啟用時刻,所在位置,訊號強度\n' +
      'A001,2026-01-15 08:30:00,高雄市苓雅區四維三路2號,-75\n' +
      'A002,2026-01-15 09:15:00,高雄市前鎮區中山二路5號,-80\n';
    await page.setInputFiles('#upl', {
      name: '怪格式.csv', mimeType: 'text/csv', buffer: Buffer.from(csv, 'utf8'),
    });
    await page.waitForTimeout(300);
    await page.locator('#btnUpload').click();

    // 等診斷 modal（系統認不得這些欄名）
    await page.locator('text=無法解析此檔案').first().waitFor({ timeout: 15000 }).catch(() => {});
    await page.locator('#__diagManual').click();
    await page.waitForTimeout(500);

    // 問答式 modal：應出現「時間在哪一欄？」
    const hasQuestion = await page.locator('text=時間在哪一欄').count();
    assert('index(guest): 手動對應為問答式（「時間在哪一欄？」）',
      hasQuestion >= 1, `count=${hasQuestion}`);

    // 依範例值（2026-01-15 08:30:00）自動猜：時間欄應預選「啟用時刻」
    const timeSel = await page.locator('select[data-field="time"]').inputValue().catch(() => '');
    assert('index(guest): 依範例值自動猜時間欄=啟用時刻',
      timeSel === '啟用時刻', `selected=${timeSel}`);
    // 依範例值（高雄市…路…號）自動猜：地址欄應預選「所在位置」
    const addrSel = await page.locator('select[data-field="addr"]').inputValue().catch(() => '');
    assert('index(guest): 依範例值自動猜地址欄=所在位置',
      addrSel === '所在位置', `selected=${addrSel}`);

    assert('index(guest): 問答式對應無 pageerror',
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
