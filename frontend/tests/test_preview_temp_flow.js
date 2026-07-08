// frontend/tests/test_preview_temp_flow.js
// ---------------------------------------------------------------------------
// P9 Phase 2A.2 —— window.CT_PREVIEW_FLOW（frontend/preview-state.js）單元測試
// + index.html 佈線的靜態守護（不啟瀏覽器）。
//
// 零依賴：只用 Node 內建（fs / path / assert）。preview-state.js 是 classic
// script IIFE，掛 global.window 後 eval 載入。純函式以注入 mock 依賴測試，
// 不需 DOM / jsdom。
//
// 執行：node frontend/tests/test_preview_temp_flow.js
// ---------------------------------------------------------------------------
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const PS_JS = path.join(__dirname, '..', 'preview-state.js');
const INDEX_HTML = path.join(__dirname, '..', 'index.html');
const SRC = fs.readFileSync(PS_JS, 'utf8');
const INDEX = fs.readFileSync(INDEX_HTML, 'utf8');

let passed = 0, failed = 0;
const failures = [];

function loadFlow() {
  global.window = {};
  eval(SRC);
  return global.window.CT_PREVIEW_FLOW;
}

async function test(name, fn) {
  try {
    await fn(loadFlow());
    passed++;
    console.log('  ✓ ' + name);
  } catch (e) {
    failed++;
    failures.push(name);
    console.log('  ✗ ' + name + '\n      ' + (e && e.message));
  }
}

// 建立 preview create response（含刻意混入的 _records 與未知欄位）。
function fakeCreateResp(previewId, over) {
  return Object.assign({
    preview_id: previewId,
    features: [{ type: 'Feature', properties: { target_id: 't' } }],
    total: 5, plotted: 4, skipped: 1,
    parser_type: 'auto', expires_at: '2026-07-05T10:00:00+08:00',
    _records: [{ secret: true }],          // 絕不可進 state
    internal: 'x',
  }, over || {});
}

function previewError(kind, message) {
  const e = new Error(message || kind);
  e.name = 'PreviewError';
  e.kind = kind;
  return e;
}

(async function run() {
  console.log('window.CT_PREVIEW_FLOW（P9 Phase 2A.2）');

  // 1. temp 自動辨識呼叫 createPreview
  await test('1. runTempPreviewUpload 呼叫注入的 createPreview', async (F) => {
    const store = new Map();
    let called = 0;
    await F.runTempPreviewUpload([{ name: 'a.xlsx' }], {
      createPreview: async () => { called++; return fakeCreateResp('p1'); },
      store,
    });
    assert.strictEqual(called, 1);
  });

  // 2. temp 成功後不寫 _tempRecordsStore（legacy 未被觸碰）
  await test('2. 成功後 legacy store 保持空', async (F) => {
    const store = new Map();
    const legacy = new Map();
    await F.runTempPreviewUpload([{ name: 'a.xlsx' }], {
      createPreview: async () => fakeCreateResp('p1'),
      store,
    });
    assert.strictEqual(legacy.size, 0, 'legacy 不應被寫入');
    assert.strictEqual(store.size, 1);
  });

  // 3. temp 成功後保存 preview_id / expires_at / 統計
  await test('3. state 保存 preview_id / expires_at / total/plotted/skipped', async (F) => {
    const store = new Map();
    await F.runTempPreviewUpload([{ name: 'a.xlsx' }], {
      createPreview: async () => fakeCreateResp('p1'),
      store,
    });
    const item = store.get('a')[0];
    assert.strictEqual(item.preview_id, 'p1');
    assert.strictEqual(item.expires_at, '2026-07-05T10:00:00+08:00');
    assert.strictEqual(item.total, 5);
    assert.strictEqual(item.plotted, 4);
    assert.strictEqual(item.skipped, 1);
    assert.strictEqual(item.status, F.STATUS.READY);
    assert.strictEqual(item.saved, false);
    assert.strictEqual(item.filename, 'a.xlsx');
  });

  // 4. response 含 _records 也不保存
  await test('4. state 不含 _records / 未知欄位', async (F) => {
    const store = new Map();
    await F.runTempPreviewUpload([{ name: 'a.xlsx' }], {
      createPreview: async () => fakeCreateResp('p1'),
      store,
    });
    const item = store.get('a')[0];
    assert.strictEqual(item._records, undefined);
    assert.strictEqual(item.internal, undefined);
    assert.strictEqual(item.features, undefined);
  });

  // 5. 多檔同 target → 多個 preview item，不互相覆蓋
  await test('5. 同 target 多檔 → 陣列 append 不覆蓋', async (F) => {
    const store = new Map();
    let n = 0;
    await F.runTempPreviewUpload([{ name: 'x.xlsx' }, { name: 'x.xlsx' }], {
      createPreview: async () => fakeCreateResp('p' + (++n)),
      store,
      targetIdOf: () => 'sameTarget',
    });
    const items = store.get('sameTarget');
    assert.strictEqual(items.length, 2);
    assert.strictEqual(items[0].preview_id, 'p1');
    assert.strictEqual(items[1].preview_id, 'p2');
  });

  // 6. create 422 diagnosis → 進 mapping fallback，不留 preview state
  await test('6. diagnosis → onDiagnosis 呼叫且不建 state', async (F) => {
    const store = new Map();
    let diagFile = null;
    const summary = await F.runTempPreviewUpload([{ name: 'weird.xlsx' }], {
      createPreview: async () => { throw previewError('diagnosis', '無法辨識'); },
      store,
      onDiagnosis: (file) => { diagFile = file; },
    });
    assert.strictEqual(store.size, 0, '不得殘留 preview state');
    assert.strictEqual(summary.items.length, 0);
    assert.strictEqual(summary.diagnoses.length, 1);
    assert.ok(diagFile);
  });

  // 7. mapping fallback 分路（diagnosis 不進 preview，交回 onError 以外的 onDiagnosis）
  await test('7. 非 diagnosis 錯誤 → onError 而非 onDiagnosis', async (F) => {
    const store = new Map();
    let errCount = 0, diagCount = 0;
    await F.runTempPreviewUpload([{ name: 'big.xlsx' }], {
      createPreview: async () => { throw previewError('too_large'); },
      store,
      onDiagnosis: () => { diagCount++; },
      onError: () => { errCount++; },
    });
    assert.strictEqual(diagCount, 0);
    assert.strictEqual(errCount, 1);
    assert.strictEqual(store.size, 0);
  });

  // 8. guest flow 不呼叫 CT_PREVIEW（chooseUploadMode）
  await test('8. chooseUploadMode: guest / temp-preview / project / ask', (F) => {
    assert.strictEqual(F.chooseUploadMode({ hasToken: false, sessionMode: 'temp' }), 'guest');
    assert.strictEqual(F.chooseUploadMode({ hasToken: true, sessionMode: 'temp' }), 'temp-preview');
    assert.strictEqual(F.chooseUploadMode({ hasToken: true, sessionMode: 'project' }), 'project');
    assert.strictEqual(F.chooseUploadMode({ hasToken: true, sessionMode: null }), 'ask');
  });

  // 9. save modal 對 preview item 呼叫 savePreview
  await test('9. runPreviewSaveForTarget 呼叫 savePreview', async (F) => {
    const store = new Map();
    store.set('a', [F.buildPreviewItem(fakeCreateResp('p1'), 'a.xlsx')]);
    let args = null;
    const res = await F.runPreviewSaveForTarget('a', 'PROJ', {
      savePreview: async (pid, proj, tid) => { args = [pid, proj, tid]; return { evidence_id: 7, inserted: 4, skipped: 1 }; },
      store,
    });
    assert.deepStrictEqual(args, ['p1', 'PROJ', 'a']);
    assert.strictEqual(res.successes, 1);
    assert.strictEqual(res.insertedTotal, 4);
    assert.strictEqual(store.get('a')[0].saved, true);
    assert.strictEqual(store.get('a')[0].status, F.STATUS.SAVED);
    assert.strictEqual(store.get('a')[0].evidence_id, 7);
  });

  // 9b. rename：saveAsTargetId 生效
  await test('9b. saveAsTargetId → savePreview 收到新 target 名', async (F) => {
    const store = new Map();
    store.set('orig', [F.buildPreviewItem(fakeCreateResp('p1'), 'orig.xlsx')]);
    let sentTid = null;
    await F.runPreviewSaveForTarget('orig', 'PROJ', {
      savePreview: async (pid, proj, tid) => { sentTid = tid; return { evidence_id: 1, inserted: 1, skipped: 0 }; },
      store,
    }, 'renamed');
    assert.strictEqual(sentTid, 'renamed');
  });

  // 10. preview save 不涉及 records（savePreview 僅 3 個純量參數）
  await test('10. savePreview 只收 (preview_id, project, target)，無 records', async (F) => {
    const store = new Map();
    store.set('a', [F.buildPreviewItem(fakeCreateResp('p1'), 'a.xlsx')]);
    let argc = null, hasArray = false;
    await F.runPreviewSaveForTarget('a', 'PROJ', {
      savePreview: async function () { argc = arguments.length; for (const a of arguments) if (Array.isArray(a)) hasArray = true; return { evidence_id: 1, inserted: 1, skipped: 0 }; },
      store,
    });
    assert.strictEqual(argc, 3);
    assert.strictEqual(hasArray, false, '不得傳入 records 陣列');
  });

  // 11. manual mapping records → classify=legacy
  await test('11. classifyTargetSave: 只有 legacy → legacy', (F) => {
    const preview = new Map();
    const legacy = new Map([['a', [{ x: 1 }]]]);
    assert.strictEqual(F.classifyTargetSave('a', preview, legacy), 'legacy');
  });

  // 12. 同 target 兩者並存 → conflict
  await test('12. classifyTargetSave: preview + legacy → conflict', (F) => {
    const preview = new Map([['a', [F.buildPreviewItem(fakeCreateResp('p1'), 'a')]]]);
    const legacy = new Map([['a', [{ x: 1 }]]]);
    assert.strictEqual(F.classifyTargetSave('a', preview, legacy), 'conflict');
    assert.strictEqual(F.classifyTargetSave('b', preview, legacy), 'empty');
    assert.strictEqual(F.classifyTargetSave('a', new Map([['a', [F.buildPreviewItem(fakeCreateResp('p'), 'a')]]]), new Map()), 'preview');
  });

  // 13. 已 saved preview 不重送
  await test('13. 已 saved 的 item 不重送', async (F) => {
    const store = new Map();
    const item = F.buildPreviewItem(fakeCreateResp('p1'), 'a');
    item.saved = true; item.status = F.STATUS.SAVED;
    store.set('a', [item]);
    let called = 0;
    const res = await F.runPreviewSaveForTarget('a', 'PROJ', {
      savePreview: async () => { called++; return {}; },
      store,
    });
    assert.strictEqual(called, 0, 'saved 不應再呼叫 savePreview');
    assert.strictEqual(res.alreadySaved, 1);
    assert.strictEqual(res.successes, 0);
  });

  // 14. 部分成功時只標成功項目
  await test('14. 部分失敗：只成功項標 saved', async (F) => {
    const store = new Map();
    store.set('a', [
      F.buildPreviewItem(fakeCreateResp('p1'), 'a'),
      F.buildPreviewItem(fakeCreateResp('p2'), 'a'),
    ]);
    const res = await F.runPreviewSaveForTarget('a', 'PROJ', {
      savePreview: async (pid) => {
        if (pid === 'p2') throw previewError('generic', 'boom');
        return { evidence_id: 1, inserted: 2, skipped: 0 };
      },
      store,
    });
    assert.strictEqual(res.successes, 1);
    assert.strictEqual(res.failures, 1);
    assert.strictEqual(store.get('a')[0].saved, true);
    assert.strictEqual(store.get('a')[1].saved, false);
    assert.strictEqual(store.get('a')[1].status, F.STATUS.READY, '可重試');
  });

  // 15. consumed 410 → saved/consumed state
  await test('15. save 遇 consumed → saved=true, status=consumed', async (F) => {
    const store = new Map();
    store.set('a', [F.buildPreviewItem(fakeCreateResp('p1'), 'a')]);
    const res = await F.runPreviewSaveForTarget('a', 'PROJ', {
      savePreview: async () => { throw previewError('consumed'); },
      store,
    });
    assert.strictEqual(store.get('a')[0].saved, true);
    assert.strictEqual(store.get('a')[0].status, F.STATUS.CONSUMED);
    assert.strictEqual(res.alreadySaved, 1);
    assert.strictEqual(res.failures, 0);
  });

  // 16 & 17. expired / revoked → 對應 state（applyErrorToItem + save 路徑）
  await test('16. applyErrorToItem expired → EXPIRED', (F) => {
    const item = F.buildPreviewItem(fakeCreateResp('p1'), 'a');
    F.applyErrorToItem(item, 'expired');
    assert.strictEqual(item.status, F.STATUS.EXPIRED);
    assert.strictEqual(item.saved, false);
  });
  await test('17. applyErrorToItem revoked → REVOKED；save 遇 expired 標 EXPIRED', async (F) => {
    const item = F.buildPreviewItem(fakeCreateResp('p1'), 'a');
    F.applyErrorToItem(item, 'revoked');
    assert.strictEqual(item.status, F.STATUS.REVOKED);
    const store = new Map();
    store.set('a', [F.buildPreviewItem(fakeCreateResp('p2'), 'a')]);
    await F.runPreviewSaveForTarget('a', 'PROJ', {
      savePreview: async () => { throw previewError('expired'); },
      store,
    });
    assert.strictEqual(store.get('a')[0].status, F.STATUS.EXPIRED);
    assert.strictEqual(store.get('a')[0].saved, false);
  });

  // 18. too_large → 正式上傳提示
  await test('18. previewErrorMessage too_large → 引導正式上傳', (F) => {
    const msg = F.previewErrorMessage(previewError('too_large', 'server said something'));
    assert.ok(msg.indexOf('正式上傳') !== -1);
  });

  // 19. no_key → 系統設定提示
  await test('19. previewErrorMessage no_key → 系統管理員', (F) => {
    const msg = F.previewErrorMessage(previewError('no_key'));
    assert.ok(msg.indexOf('系統管理員') !== -1);
  });

  // 20. clear/reset 只 revoke 尚未 saved 的 preview
  await test('20. runRevokePending 只撤銷 READY 且未 saved', async (F) => {
    const store = new Map();
    const ready = F.buildPreviewItem(fakeCreateResp('p1'), 'a');
    const savedItem = F.buildPreviewItem(fakeCreateResp('p2'), 'b'); savedItem.saved = true; savedItem.status = F.STATUS.SAVED;
    const consumedItem = F.buildPreviewItem(fakeCreateResp('p3'), 'c'); consumedItem.saved = true; consumedItem.status = F.STATUS.CONSUMED;
    store.set('a', [ready]); store.set('b', [savedItem]); store.set('c', [consumedItem]);
    const revoked = [];
    const res = await F.runRevokePending(store, { revokePreview: async (pid) => { revoked.push(pid); } });
    assert.deepStrictEqual(revoked, ['p1']);
    assert.strictEqual(res.revoked, 1);
    assert.strictEqual(ready.status, F.STATUS.REVOKED);
  });

  // 21. saved/consumed preview 不被 collectPendingPreviews 收集
  await test('21. collectPendingPreviews 排除 saved/consumed', (F) => {
    const store = new Map();
    const ready = F.buildPreviewItem(fakeCreateResp('p1'), 'a');
    const saved = F.buildPreviewItem(fakeCreateResp('p2'), 'b'); saved.saved = true; saved.status = F.STATUS.SAVED;
    store.set('a', [ready]); store.set('b', [saved]);
    const pending = F.collectPendingPreviews(store);
    assert.strictEqual(pending.length, 1);
    assert.strictEqual(pending[0].item.preview_id, 'p1');
  });

  // 21b. revoke 失敗不阻塞
  await test('21b. revoke 失敗收集於 failed，不拋', async (F) => {
    const store = new Map();
    store.set('a', [F.buildPreviewItem(fakeCreateResp('p1'), 'a')]);
    const res = await F.runRevokePending(store, { revokePreview: async () => { throw new Error('net'); } });
    assert.strictEqual(res.revoked, 0);
    assert.deepStrictEqual(res.failed, ['p1']);
  });

  // 22. 不在 beforeunload 自動 revoke（index.html 靜態守護）
  await test('22. index.html 不在 beforeunload 呼叫 revoke', () => {
    // 允許存在 beforeunload（其他用途），但不得在其中呼叫 revokePreview / revokePendingPreviews。
    const re = /addEventListener\(\s*['"]beforeunload['"][\s\S]{0,400}?(revokePreview|revokePendingPreviews)/;
    assert.strictEqual(re.test(INDEX), false, 'beforeunload 內不得自動 revoke');
  });

  // 23. 錯誤輸出不含 token（previewErrorMessage 對映射 kind 回靜態字串）
  await test('23. previewErrorMessage 映射 kind 忽略 err.message（不透傳可能含敏感資訊）', (F) => {
    const leaky = previewError('too_large', 'Bearer SUPER-SECRET');
    const msg = F.previewErrorMessage(leaky);
    assert.strictEqual(msg.indexOf('SUPER-SECRET'), -1);
    assert.strictEqual(msg.indexOf('Bearer'), -1);
  });

  // 24. 既有 guest / sessionStorage 行為未回歸（index.html 靜態守護）
  await test('24. index.html 仍保留 guest / sessionStorage 舊行為', () => {
    assert.ok(INDEX.indexOf('doGuestUpload') !== -1, 'doGuestUpload 應仍存在');
    assert.ok(INDEX.indexOf('ct_guest_store') !== -1, 'guest sessionStorage 應仍存在');
    assert.ok(INDEX.indexOf('parse-only') !== -1, 'guest parse-only 應仍存在');
    // guest 路徑不得改呼叫 CT_PREVIEW.createPreview（doGuestUpload 內不出現）
    const guestFn = INDEX.slice(INDEX.indexOf('async function doGuestUpload'));
    const guestBody = guestFn.slice(0, guestFn.indexOf('\n    }\n'));
    assert.strictEqual(guestBody.indexOf('CT_PREVIEW'), -1, 'guest flow 不得呼叫 CT_PREVIEW');
  });

  // 25. index.html 佈線守護：doTempUpload 走 createPreview、載入 preview-state.js
  await test('25. index.html 佈線：script 載入 + doTempUpload 用 createPreview', () => {
    assert.ok(INDEX.indexOf('./preview-state.js') !== -1, '應載入 preview-state.js');
    const tempFn = INDEX.slice(INDEX.indexOf('async function doTempUpload'));
    const tempBody = tempFn.slice(0, tempFn.indexOf('\n    }\n'));
    assert.ok(tempBody.indexOf('runTempPreviewUpload') !== -1, 'doTempUpload 應走 runTempPreviewUpload');
    assert.ok(tempBody.indexOf('CT_PREVIEW.createPreview') !== -1, 'doTempUpload 應呼叫 createPreview');
    assert.strictEqual(tempBody.indexOf('_tempRecordsStore'), -1, 'doTempUpload 不得寫 _tempRecordsStore');
    assert.strictEqual(tempBody.indexOf('upload/parse-temp'), -1, 'doTempUpload 不得再打 /upload/parse-temp 端點');
  });

  // ═══ P9 Phase 2A.3 收尾：withRequestId ═══

  // 26. 有 request_id → 附錯誤追蹤碼
  await test('26. withRequestId 有 request_id → 附追蹤碼', (F) => {
    const out = F.withRequestId('儲存失敗', { request_id: 'req_abc123' });
    assert.ok(out.indexOf('req_abc123') !== -1);
    assert.ok(out.indexOf('錯誤追蹤碼') !== -1);
    assert.ok(out.indexOf('儲存失敗') === 0, '原訊息應在前');
  });

  // 27. 無 request_id → 原樣（不追加空字串）
  await test('27. withRequestId 無 request_id → 原樣回傳', (F) => {
    assert.strictEqual(F.withRequestId('X', { request_id: null }), 'X');
    assert.strictEqual(F.withRequestId('X', {}), 'X');
    assert.strictEqual(F.withRequestId('X', null), 'X');
    assert.strictEqual(F.withRequestId('X', undefined), 'X');
  });

  // 28. request_id 不會被當 HTML 執行（純文字回傳，交由 textContent 呈現）
  await test('28. withRequestId 純文字回傳、不 HTML 包裝', (F) => {
    const evil = '<img src=x onerror=alert(1)>';
    const out = F.withRequestId('錯誤', { request_id: evil });
    // 回傳為純字串、原樣夾帶（呼叫端以 textContent 呈現 → 不會被當 HTML 執行）
    assert.strictEqual(typeof out, 'string');
    assert.ok(out.indexOf(evil) !== -1, '原樣夾帶，不轉義也不包 HTML tag');
    assert.strictEqual(out.indexOf('<div'), -1);
    assert.strictEqual(out.indexOf('innerHTML'), -1);
  });

  // 29. 訊息不含 token / Authorization
  await test('29. withRequestId 不引入 token / Authorization', (F) => {
    const out = F.withRequestId('權限不足', { request_id: 'req_1', message: 'x' });
    assert.strictEqual(out.indexOf('Bearer'), -1);
    assert.strictEqual(out.indexOf('Authorization'), -1);
    assert.strictEqual(out.indexOf('token'), -1);
  });

  // 30. index.html 靜態守護：preview 錯誤 glue 用 withRequestId + showToast/textContent（非 innerHTML）
  await test('30. index.html 錯誤 glue 用 withRequestId + 安全輸出', () => {
    assert.ok(INDEX.indexOf('_PF.withRequestId') !== -1, 'index.html 應呼叫 withRequestId');
    // onError 走 showToast（其以 textContent 輸出），不得用 innerHTML 直塞 request_id
    const tempFn = INDEX.slice(INDEX.indexOf('async function doTempUpload'));
    const tempBody = tempFn.slice(0, tempFn.indexOf('\n    }\n'));
    assert.ok(tempBody.indexOf('withRequestId') !== -1, 'onError 應包 withRequestId');
    assert.ok(tempBody.indexOf('showToast') !== -1, 'onError 應用 showToast');
  });

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  if (failed > 0) {
    console.log('\nFailures:');
    failures.forEach((n) => console.log('  - ' + n));
    process.exit(1);
  }
  process.exit(0);
})();
