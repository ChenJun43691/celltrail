// frontend/tests/test_preview_client.js
// ---------------------------------------------------------------------------
// P9 Phase 2A.1 —— window.CT_PREVIEW（frontend/api.js）單元測試。
//
// 零依賴：只用 Node 內建（fs / path / assert），不引入任何 npm 套件、
// 不啟 jsdom、不打真實雲端 API。fetch / FormData 為 Node 18+ 內建 global，
// fetch 逐測試以 mock 覆寫。
//
// 執行：node frontend/tests/test_preview_client.js
//
// 載入方式：api.js 是 classic-script IIFE，讀寫 window/location 全域。
// Node 中把這些掛上 global（global 屬性即 bare 全域），再 eval 檔案內容，
// IIFE 執行後即在 global.window 上掛好 CT_API / CT_PREVIEW。
// ---------------------------------------------------------------------------
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const API_JS = path.join(__dirname, '..', 'api.js');
const SRC = fs.readFileSync(API_JS, 'utf8');

// ── 測試工具 ────────────────────────────────────────────────
let passed = 0;
let failed = 0;
const failures = [];

function test(name, fn) {
  // 每個測試前重置 global 環境，避免互相污染。
  resetEnv();
  try {
    const r = fn();
    if (r && typeof r.then === 'function') {
      throw new Error('test 函式不應回傳 promise（請用同步斷言或 await 展開）');
    }
    passed++;
    console.log('  ✓ ' + name);
  } catch (e) {
    failed++;
    failures.push({ name, err: e });
    console.log('  ✗ ' + name + '\n      ' + (e && e.message));
  }
}

async function testAsync(name, fn) {
  resetEnv();
  try {
    await fn();
    passed++;
    console.log('  ✓ ' + name);
  } catch (e) {
    failed++;
    failures.push({ name, err: e });
    console.log('  ✗ ' + name + '\n      ' + (e && e.message));
  }
}

// ── mock 環境 ───────────────────────────────────────────────
let tokenStore = {};

function resetEnv() {
  tokenStore = {};
  global.window = {};
  global.location = { hostname: 'localhost' };
  global.localStorage = {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(tokenStore, k) ? tokenStore[k] : null),
    setItem: (k, v) => { tokenStore[k] = String(v); },
    removeItem: (k) => { delete tokenStore[k]; },
  };
  // 預設 fetch 直接失敗，強制每個需要網路的測試自行安裝 mock。
  global.fetch = () => { throw new Error('fetch not mocked'); };
}

function loadPreview() {
  // eval 於此函式作用域；window/location/localStorage 皆為 global 屬性 → bare 可解析。
  eval(SRC);
  return global.window.CT_PREVIEW;
}

function setToken(t) { tokenStore['ct_token'] = t; }

// 安裝 mock fetch，回傳呼叫記錄陣列。
function mockFetch(resp) {
  const calls = [];
  global.fetch = async (url, opts) => {
    calls.push({ url, opts: opts || {} });
    if (resp && resp.throwNetwork) throw new Error('簡易網路錯誤 boundary=SECRET-SHOULD-NOT-LEAK');
    const status = resp.status;
    const hasBody = resp.body !== undefined;
    return {
      ok: status >= 200 && status < 300,
      status: status,
      text: async () => (hasBody ? JSON.stringify(resp.body) : ''),
    };
  };
  return calls;
}

// ── 測試 ────────────────────────────────────────────────────
(async function run() {
  console.log('window.CT_PREVIEW（P9 Phase 2A.1）');

  // 匯出面 —— sanity
  test('exports 齊全（6 個公開函式）', () => {
    const P = loadPreview();
    ['createPreview', 'getPreview', 'sealPreview', 'savePreview', 'revokePreview', 'parsePreviewError']
      .forEach((k) => assert.strictEqual(typeof P[k], 'function', 'missing ' + k));
    assert.strictEqual(typeof P.sanitizePreviewResponse, 'function');
    // 既有 CT_API 未被破壞
    assert.strictEqual(global.window.CT_API, 'http://localhost:8000/api');
    assert.strictEqual(global.window.CT_API_BASE, 'http://localhost:8000');
  });

  // 1. createPreview 正確建立 FormData
  await testAsync('1. createPreview 建立含 file 的 FormData', async () => {
    setToken('T1');
    const calls = mockFetch({ status: 200, body: { preview_id: 'p1', features: [] } });
    const P = loadPreview();
    await P.createPreview('FAKEFILE', 'tgt');
    const body = calls[0].opts.body;
    assert.ok(body instanceof FormData, 'body 應為 FormData');
    assert.strictEqual(body.has('file'), true, '應含 file 欄位');
  });

  // 2. targetId 空值時不送 target_id
  await testAsync('2. targetId 空字串 → 不送 target_id', async () => {
    setToken('T1');
    const calls = mockFetch({ status: 200, body: { preview_id: 'p1' } });
    const P = loadPreview();
    await P.createPreview('FAKEFILE', '');
    assert.strictEqual(calls[0].opts.body.has('target_id'), false);
    // undefined 亦不送
    const calls2 = mockFetch({ status: 200, body: { preview_id: 'p1' } });
    await P.createPreview('FAKEFILE');
    assert.strictEqual(calls2[0].opts.body.has('target_id'), false);
  });

  // 3. targetId 有值時正確送出
  await testAsync('3. targetId 有值 → 正確送出', async () => {
    setToken('T1');
    const calls = mockFetch({ status: 200, body: { preview_id: 'p1' } });
    const P = loadPreview();
    await P.createPreview('FAKEFILE', 'target-A');
    assert.strictEqual(calls[0].opts.body.get('target_id'), 'target-A');
  });

  // 4. create response 不暴露 _records
  await testAsync('4. create response 濾除 _records', async () => {
    setToken('T1');
    mockFetch({ status: 200, body: { preview_id: 'p1', features: [{ x: 1 }], total: 3, plotted: 2, skipped: 1, parser_type: 'auto', expires_at: 'Z', _records: [{ secret: true }] } });
    const P = loadPreview();
    const out = await P.createPreview('FAKEFILE', 't');
    assert.strictEqual(out._records, undefined, '不得含 _records');
    assert.strictEqual(out.preview_id, 'p1');
    assert.strictEqual(out.plotted, 2);
    assert.ok(Array.isArray(out.features));
  });

  // 5. read response 不暴露未知欄位
  await testAsync('5. read response 只留 allowlist', async () => {
    setToken('T1');
    mockFetch({ status: 200, body: { features: [], total: 1, plotted: 1, skipped: 0, _records: [1], internal_secret: 'x' } });
    const P = loadPreview();
    const out = await P.getPreview('p1');
    assert.deepStrictEqual(Object.keys(out).sort(), ['features', 'plotted', 'skipped', 'total']);
    assert.strictEqual(out._records, undefined);
    assert.strictEqual(out.internal_secret, undefined);
  });

  // 6. save body 只包含 project_id / target_id
  await testAsync('6. save body 只含 project_id/target_id', async () => {
    setToken('T1');
    const calls = mockFetch({ status: 200, body: { ok: true, evidence_id: 9 } });
    const P = loadPreview();
    await P.savePreview('p1', 'PROJ', 'TID');
    const sent = JSON.parse(calls[0].opts.body);
    assert.deepStrictEqual(Object.keys(sent).sort(), ['project_id', 'target_id']);
    assert.strictEqual(sent.project_id, 'PROJ');
    assert.strictEqual(sent.target_id, 'TID');
    assert.strictEqual(calls[0].opts.headers['Content-Type'], 'application/json');
  });

  // 7. save 不要求前置 seal
  await testAsync('7. savePreview 不需先呼叫 sealPreview', async () => {
    setToken('T1');
    mockFetch({ status: 200, body: { ok: true, evidence_id: 1, sha256_full: 'h', total: 2, inserted: 2, skipped: 0 } });
    const P = loadPreview();
    const out = await P.savePreview('p1', 'PROJ', '');
    assert.strictEqual(out.ok, true);
    assert.strictEqual(out.evidence_id, 1);
  });

  // 8. revoke 使用 DELETE
  await testAsync('8. revokePreview 使用 DELETE', async () => {
    setToken('T1');
    const calls = mockFetch({ status: 200, body: { ok: true } });
    const P = loadPreview();
    await P.revokePreview('p1');
    assert.strictEqual(calls[0].opts.method, 'DELETE');
    assert.ok(/\/api\/preview\/p1$/.test(calls[0].url));
  });

  // 9. Authorization header 正確加入
  await testAsync('9. Authorization header = Bearer <token>', async () => {
    setToken('SECRET-TOKEN-9');
    const calls = mockFetch({ status: 200, body: { features: [] } });
    const P = loadPreview();
    await P.getPreview('p1');
    assert.strictEqual(calls[0].opts.headers['Authorization'], 'Bearer SECRET-TOKEN-9');
  });

  // 10. TOKEN 缺失時不發 request
  await testAsync('10. 無 token → 不發 request 且拋 auth_required', async () => {
    // 不 setToken
    const calls = mockFetch({ status: 200, body: {} });
    const P = loadPreview();
    let thrown = null;
    try { await P.getPreview('p1'); } catch (e) { thrown = e; }
    assert.ok(thrown, '應拋錯');
    assert.strictEqual(thrown.kind, 'auth_required');
    assert.strictEqual(calls.length, 0, '不得發出 request');
  });

  // 11–19 status → kind mapping（透過 client 觸發）
  const statusCases = [
    ['11. 401 → auth_required', 401, {}, 'auth_required'],
    ['12. 403 → forbidden', 403, { detail: 'x' }, 'forbidden'],
    ['13. 404 → not_found', 404, { detail: 'preview 不存在' }, 'not_found'],
    ['14. 410 過期 → expired', 410, { detail: 'preview 已過期' }, 'expired'],
    ['15. 410 撤銷 → revoked', 410, { detail: 'preview 已撤銷' }, 'revoked'],
    ['16. 410 存證 → consumed', 410, { detail: 'preview 已存證，不可重複使用' }, 'consumed'],
    ['17. 413 → too_large', 413, { detail: 'preview 暫不支援中大檔' }, 'too_large'],
    ['18. 409 → sha_mismatch', 409, { detail: '檔案指紋不符，拒絕存證' }, 'sha_mismatch'],
    ['19. 503 金鑰 → no_key', 503, { detail: 'preview 加密金鑰未設定，暫時無法建立 preview' }, 'no_key'],
  ];
  for (const [name, status, body, kind] of statusCases) {
    await testAsync(name, async () => {
      setToken('T1');
      mockFetch({ status, body });
      const P = loadPreview();
      let thrown = null;
      try { await P.getPreview('p1'); } catch (e) { thrown = e; }
      assert.ok(thrown, '應拋 PreviewError');
      assert.strictEqual(thrown.name, 'PreviewError');
      assert.strictEqual(thrown.kind, kind, 'kind 應為 ' + kind + ' 而非 ' + thrown.kind);
      assert.strictEqual(thrown.status, status);
    });
  }

  // 20. 422 diagnosis 保留 diagnosis
  await testAsync('20. 422 diagnosis → kind=diagnosis 並保留 diagnosis', async () => {
    setToken('T1');
    const diag = { available_columns: ['A', 'B'], missing: ['start_ts'] };
    mockFetch({ status: 422, body: { ok: false, error: 'format_unknown', detail: '無法辨識', diagnosis: diag } });
    const P = loadPreview();
    let thrown = null;
    try { await P.createPreview('FAKEFILE', 't'); } catch (e) { thrown = e; }
    assert.ok(thrown);
    assert.strictEqual(thrown.kind, 'diagnosis');
    assert.deepStrictEqual(thrown.diagnosis, diag);
  });

  // 21. encrypted file → encrypted_file
  await testAsync('21. 422 密碼保護檔 → encrypted_file', async () => {
    setToken('T1');
    mockFetch({ status: 422, body: { detail: '此檔案有密碼保護（加密），系統無法讀取。請在 Excel 中移除密碼後，另存為一般 .xlsx 再重新上傳。' } });
    const P = loadPreview();
    let thrown = null;
    try { await P.createPreview('FAKEFILE', 't'); } catch (e) { thrown = e; }
    assert.ok(thrown);
    assert.strictEqual(thrown.kind, 'encrypted_file');
    // encrypted_file 用後端 detail 當訊息
    assert.ok(thrown.message.indexOf('密碼保護') !== -1);
  });

  // 22. network error → network
  await testAsync('22. 網路錯誤 → kind=network', async () => {
    setToken('T1');
    mockFetch({ throwNetwork: true });
    const P = loadPreview();
    let thrown = null;
    try { await P.getPreview('p1'); } catch (e) { thrown = e; }
    assert.ok(thrown);
    assert.strictEqual(thrown.kind, 'network');
  });

  // 23. 錯誤內容不得包含 token（也不得含網路例外中夾帶的敏感字串）
  await testAsync('23. 錯誤內容不外洩 token / boundary', async () => {
    setToken('SUPER-SECRET-TOKEN-XYZ');
    // 先測 403（有 token 的請求）
    mockFetch({ status: 403, body: { detail: 'x' } });
    const P = loadPreview();
    let thrown = null;
    try { await P.getPreview('p1'); } catch (e) { thrown = e; }
    const dump = JSON.stringify({ m: thrown.message, k: thrown.kind, s: thrown.status }) + ' ' + String(thrown.stack || '');
    assert.strictEqual(dump.indexOf('SUPER-SECRET-TOKEN-XYZ'), -1, 'error 不得含 token');
    // 網路錯誤也不得洩漏底層 e.message（其含 SECRET-SHOULD-NOT-LEAK）
    mockFetch({ throwNetwork: true });
    let net = null;
    try { await P.getPreview('p1'); } catch (e) { net = e; }
    assert.strictEqual(net.message.indexOf('SECRET-SHOULD-NOT-LEAK'), -1, 'network error 不得夾帶底層訊息');
  });

  // 24. 原 response object 不被 mutate
  test('24. sanitizePreviewResponse 不 mutate 來源、不含 _records', () => {
    const P = loadPreview();
    const orig = { preview_id: 'p1', features: [{ a: 1 }], _records: [{ secret: 1 }], junk: 'x' };
    const out = P.sanitizePreviewResponse(orig, 'create');
    // 來源不變
    assert.deepStrictEqual(Object.prototype.hasOwnProperty.call(orig, '_records'), true);
    assert.strictEqual(orig._records.length, 1);
    // 輸出過濾
    assert.notStrictEqual(out, orig);
    assert.strictEqual(out._records, undefined);
    assert.strictEqual(out.junk, undefined);
    assert.strictEqual(out.preview_id, 'p1');
  });

  // ═══ P9 Phase 2A.3：machine-readable error contract ═══

  // 28. machine-readable code 優先於 status/detail
  await testAsync('28. body.error.code 優先判定 kind', async () => {
    setToken('T1');
    // status 410 但 code 明確給 REVOKED（若靠 status+detail 會判 generic）
    mockFetch({ status: 410, body: { error: { code: 'PREVIEW_REVOKED', message: '預覽已撤銷。', details: {} }, request_id: 'req_abc' } });
    const P = loadPreview();
    let thrown = null;
    try { await P.getPreview('p1'); } catch (e) { thrown = e; }
    assert.strictEqual(thrown.kind, 'revoked');
    assert.strictEqual(thrown.code, 'PREVIEW_REVOKED');
  });

  // 28b. 410 三態全靠 code（不需中文字串）
  await testAsync('28b. 410 三態靠 code：expired/revoked/consumed', async () => {
    setToken('T1');
    const P = loadPreview();
    const cases = [
      ['PREVIEW_EXPIRED', 'expired'],
      ['PREVIEW_REVOKED', 'revoked'],
      ['PREVIEW_CONSUMED', 'consumed'],
    ];
    for (const [code, kind] of cases) {
      // detail 故意留白 → 證明不靠中文字串
      mockFetch({ status: 410, body: { error: { code, message: '', details: {} }, request_id: 'req_x' } });
      let thrown = null;
      try { await P.getPreview('p1'); } catch (e) { thrown = e; }
      assert.strictEqual(thrown.kind, kind, code + ' → ' + kind);
    }
  });

  // 29. legacy detail fallback 仍可用（無 code）
  await testAsync('29. 無 code → legacy status/detail fallback', async () => {
    setToken('T1');
    mockFetch({ status: 410, body: { detail: 'preview 已過期' } });
    const P = loadPreview();
    let thrown = null;
    try { await P.getPreview('p1'); } catch (e) { thrown = e; }
    assert.strictEqual(thrown.kind, 'expired');
    assert.strictEqual(thrown.code, null);
  });

  // 30. request_id 保留於 error 物件
  await testAsync('30. error.request_id 從 body 保留', async () => {
    setToken('T1');
    mockFetch({ status: 409, body: { error: { code: 'PREVIEW_SHA_MISMATCH', message: 'x', details: {} }, request_id: 'req_trace_123' } });
    const P = loadPreview();
    let thrown = null;
    try { await P.savePreview('p1', 'proj', 't'); } catch (e) { thrown = e; }
    assert.strictEqual(thrown.request_id, 'req_trace_123');
    assert.strictEqual(thrown.kind, 'sha_mismatch');
  });

  // 31. error message 不含 token（新 contract 路徑）
  await testAsync('31. 新 contract 錯誤訊息不含 token', async () => {
    setToken('SECRET-TOKEN-XYZ');
    mockFetch({ status: 403, body: { error: { code: 'PREVIEW_FORBIDDEN', message: '你沒有權限存取這筆預覽資料。', details: {} }, request_id: 'req_1' } });
    const P = loadPreview();
    let thrown = null;
    try { await P.getPreview('p1'); } catch (e) { thrown = e; }
    const dump = JSON.stringify({ m: thrown.message, c: thrown.code, r: thrown.request_id }) + String(thrown.stack || '');
    assert.strictEqual(dump.indexOf('SECRET-TOKEN-XYZ'), -1);
  });

  // 32. AUTH_REQUIRED code → auth_required；INTERNAL_ERROR → server
  await testAsync('32. AUTH_REQUIRED/INTERNAL_ERROR/TOO_LARGE/KEY_MISSING code 映射', async () => {
    setToken('T1');
    const P = loadPreview();
    const map = [
      [401, 'AUTH_REQUIRED', 'auth_required'],
      [500, 'INTERNAL_ERROR', 'server'],
      [413, 'PREVIEW_TOO_LARGE', 'too_large'],
      [503, 'PREVIEW_KEY_MISSING', 'no_key'],
      [404, 'PREVIEW_NOT_FOUND', 'not_found'],
    ];
    for (const [status, code, kind] of map) {
      mockFetch({ status, body: { error: { code, message: 'm', details: {} }, request_id: 'r' } });
      let thrown = null;
      try { await P.getPreview('p1'); } catch (e) { thrown = e; }
      assert.strictEqual(thrown.kind, kind, code + ' → ' + kind);
    }
  });

  // 32b. PREVIEW_PARSE_FAILED + diagnosis → diagnosis（保留 diagnosis）
  await testAsync('32b. PREVIEW_PARSE_FAILED 帶 diagnosis → diagnosis', async () => {
    setToken('T1');
    const diag = { available_columns: ['A'] };
    mockFetch({ status: 422, body: { error: { code: 'PREVIEW_PARSE_FAILED', message: '無法自動辨識此檔案格式。', details: { diagnosis: diag } }, request_id: 'r' } });
    const P = loadPreview();
    let thrown = null;
    try { await P.createPreview('FAKEFILE', 't'); } catch (e) { thrown = e; }
    assert.strictEqual(thrown.kind, 'diagnosis');
    assert.deepStrictEqual(thrown.diagnosis, diag);
  });

  // 33. 所有 PreviewError 都有 code / request_id 欄位（本地產生的也是）
  await testAsync('33. 本地 auth_required 錯誤也帶 code/request_id 欄位', async () => {
    // 不 setToken → 本地拋 auth_required（不發 request）
    const P = loadPreview();
    let thrown = null;
    try { await P.getPreview('p1'); } catch (e) { thrown = e; }
    assert.strictEqual(thrown.kind, 'auth_required');
    assert.ok('code' in thrown && 'request_id' in thrown);
    assert.strictEqual(thrown.code, null);
    assert.strictEqual(thrown.request_id, null);
  });

  // ── 總結 ─────────────────────────────────────────────────
  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  if (failed > 0) {
    console.log('\nFailures:');
    failures.forEach((f) => console.log('  - ' + f.name));
    process.exit(1);
  }
  process.exit(0);
})();
