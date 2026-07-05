// frontend/preview-state.js
// ---------------------------------------------------------------------------
// P9 Phase 2A.2 —— 登入後 temp「自動辨識」預覽流程的純狀態/編排邏輯。
//
// 為什麼獨立成檔：
//   index.html 的上傳/儲存流程與 DOM 深度綁定，難以在 Node 單元測試。把「決策
//   與狀態」抽成無 DOM 依賴的純函式（依賴以參數注入：createPreview / savePreview /
//   revokePreview / onFeatures…），即可零依賴在 Node 測試（不需 jsdom）。
//   index.html 只保留薄 glue：把 DOM 事件接到這裡、把回呼接回 DOM。
//
// 範圍鎖定（Phase 2A.2）：
//   - 只服務「已登入 + _sessionMode==='temp' + 自動辨識成功」→ Preview Artifact。
//   - guest / manual-mapping 仍走舊 _tempRecordsStore（本檔不碰）。
//   - 一個 target 可對應多個 preview（多檔）→ store value 為陣列，不覆蓋前一檔。
//   - 前端「絕不」保存 `_records`：buildPreviewItem 走 allowlist，只留 metadata。
//
// classic script，掛 window.CT_PREVIEW_FLOW（與 api.js 同風格）。
// ---------------------------------------------------------------------------
(function () {
  'use strict';

  var STATUS = {
    READY:    'preview_ready',
    SAVED:    'saved',
    EXPIRED:  'expired',
    REVOKED:  'revoked',
    CONSUMED: 'consumed',
    ERROR:    'error',
  };

  function stripExt(name) {
    return String(name || '').replace(/\.[^.]+$/, '');
  }

  // preview 錯誤 kind → 使用者文案（Phase 2A.2 §8）。靜態字串，
  // 絕不夾帶 token / header / raw；未涵蓋的 kind 退回 err.message。
  var ERR_MSG = {
    too_large: '檔案超過目前預覽容量，請改用正式上傳模式。',
    no_key:    '預覽加密服務尚未完成設定，請聯絡系統管理員。',
    expired:   '預覽已過期，請重新上傳檔案。',
    revoked:   '預覽已撤銷。',
    consumed:  '預覽已完成存證，不可重複使用。',
  };
  function previewErrorMessage(err) {
    var kind = err && err.kind;
    if (kind && ERR_MSG[kind]) return ERR_MSG[kind];
    return (err && err.message) || '解析失敗';
  }

  // ── 上傳分派決策（純函式）──────────────────────────────────
  //  guest       ：未登入
  //  temp-preview：登入 + temp 模式 → 走 Preview Artifact
  //  project     ：登入 + project 模式
  //  ask         ：登入但尚未選模式
  function chooseUploadMode(ctx) {
    ctx = ctx || {};
    if (!ctx.hasToken) return 'guest';
    if (ctx.sessionMode === 'temp') return 'temp-preview';
    if (ctx.sessionMode === 'project') return 'project';
    return 'ask';
  }

  // ── preview state 建構（allowlist；絕不含 _records / features）──
  function buildPreviewItem(resp, filename) {
    resp = resp || {};
    return {
      preview_id:    resp.preview_id,
      expires_at:    resp.expires_at || null,
      total:         resp.total || 0,
      plotted:       resp.plotted || 0,
      skipped:       resp.skipped || 0,
      filename:      filename || null,
      sealed:        false,
      saved:         false,
      status:        STATUS.READY,
      evidence_id:   null,
      inserted:      null,
      saved_skipped: null,
    };
  }

  function getPreviewItems(store, targetId) {
    return (store && store.get(targetId)) || [];
  }

  // 一個 target 可有多個 preview（多檔）→ 陣列 append，不覆蓋。
  function addPreviewItem(store, targetId, item) {
    var arr = store.get(targetId);
    if (!arr) { arr = []; store.set(targetId, arr); }
    arr.push(item);
    return item;
  }

  function hasPreviewItems(store, targetId) {
    return getPreviewItems(store, targetId).length > 0;
  }

  function hasLegacyRecords(legacyStore, targetId) {
    var r = legacyStore && legacyStore.get(targetId);
    return !!(r && r.length);
  }

  // ── 儲存分派：同 target 應只屬於一種暫存 state ──────────────
  //  preview  ：只在 _previewArtifactStore
  //  legacy   ：只在 _tempRecordsStore（manual mapping / guest 舊路徑）
  //  conflict ：兩者都有 → 不可默默合併，須停止並提示
  //  empty    ：都沒有
  function classifyTargetSave(targetId, previewStore, legacyStore) {
    var p = hasPreviewItems(previewStore, targetId);
    var l = hasLegacyRecords(legacyStore, targetId);
    if (p && l) return 'conflict';
    if (p) return 'preview';
    if (l) return 'legacy';
    return 'empty';
  }

  // 依 parsePreviewError 的 kind 更新單筆 item 狀態。
  function applyErrorToItem(item, kind) {
    if (kind === 'expired') {
      item.status = STATUS.EXPIRED;
    } else if (kind === 'revoked') {
      item.status = STATUS.REVOKED;
    } else if (kind === 'consumed') {
      item.status = STATUS.CONSUMED;
      item.saved = true;   // 已存證，不可重複使用
    } else {
      item.status = STATUS.ERROR;
    }
    return item;
  }

  // ── temp 自動辨識上傳編排 ──────────────────────────────────
  // deps: { createPreview(file,targetId)->resp, store,
  //         onFeatures(features), onDiagnosis(file,err), onError(file,err),
  //         targetIdOf?(file) }
  // 回傳彙總；建立失敗（含 422 diagnosis）不留任何半成品 preview state。
  async function runTempPreviewUpload(files, deps) {
    var summary = {
      totalTotal: 0, plottedTotal: 0, skippedTotal: 0,
      items: [], diagnoses: [], failures: [],
    };
    var list = files || [];
    for (var i = 0; i < list.length; i++) {
      var file = list[i];
      var targetId = deps.targetIdOf ? deps.targetIdOf(file) : stripExt(file && file.name);
      try {
        var resp = await deps.createPreview(file, targetId);
        // 只取 allowlist 欄位進 state；features 只交給渲染回呼、不保存。
        var item = buildPreviewItem(resp, file && file.name);
        addPreviewItem(deps.store, targetId, item);
        if (deps.onFeatures) deps.onFeatures((resp && resp.features) || []);
        summary.totalTotal   += item.total;
        summary.plottedTotal += item.plotted;
        summary.skippedTotal += item.skipped;
        summary.items.push({ targetId: targetId, item: item });
      } catch (err) {
        if (err && err.kind === 'diagnosis') {
          // 自動辨識失敗 → 回退舊 manual mapping 流程（不建 preview state）。
          summary.diagnoses.push({ file: file, err: err });
          if (deps.onDiagnosis) deps.onDiagnosis(file, err);
        } else {
          summary.failures.push({ file: file, err: err });
          if (deps.onError) deps.onError(file, err);
        }
      }
    }
    return summary;
  }

  // ── preview 儲存編排（單一 target，可多筆 preview）──────────
  // deps: { savePreview(previewId,projectId,targetId)->result, store }
  //  - 不送 records；已 saved 不重送；部分失敗只標成功項。
  //  - saveAsTargetId：儲存時允許改名（modal 可改 target 名）；store 仍以
  //    原 targetId 為 key，送後端的 target_id 用 saveAsTargetId（預設同 key）。
  async function runPreviewSaveForTarget(targetId, projectId, deps, saveAsTargetId) {
    var items = getPreviewItems(deps.store, targetId);
    var sendTid = (saveAsTargetId === undefined || saveAsTargetId === null) ? targetId : saveAsTargetId;
    var res = {
      successes: 0, failures: 0, alreadySaved: 0,
      insertedTotal: 0, skippedTotal: 0, errors: [],
    };
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (item.saved) { res.alreadySaved++; continue; }            // 不重送
      if (item.status === STATUS.EXPIRED || item.status === STATUS.REVOKED) {
        res.failures++;
        res.errors.push({ preview_id: item.preview_id, kind: item.status });
        continue;
      }
      try {
        var r = await deps.savePreview(item.preview_id, projectId, sendTid);
        item.saved = true;
        item.sealed = true;                 // 後端 save inline analyst seal
        item.status = STATUS.SAVED;
        item.evidence_id = r ? r.evidence_id : null;
        item.inserted = r ? r.inserted : null;
        item.saved_skipped = r ? r.skipped : null;
        res.successes++;
        res.insertedTotal += (r && r.inserted) || 0;
        res.skippedTotal  += (r && r.skipped) || 0;
      } catch (err) {
        var kind = err && err.kind;
        if (kind === 'consumed') {
          item.saved = true; item.status = STATUS.CONSUMED;
          res.alreadySaved++;               // 已存證，不算新寫入
        } else if (kind === 'expired' || kind === 'revoked') {
          item.status = kind;
          res.failures++;
          res.errors.push({ preview_id: item.preview_id, kind: kind });
        } else {
          // 其餘錯誤：保留 READY 狀態，允許重試
          res.failures++;
          res.errors.push({ preview_id: item.preview_id, kind: kind || 'generic' });
        }
      }
    }
    return res;
  }

  // ── 撤銷尚未存證的 preview（明確取消/重置時）────────────────
  function collectPendingPreviews(store) {
    var out = [];
    store.forEach(function (items, targetId) {
      (items || []).forEach(function (item) {
        if (item.status === STATUS.READY && !item.saved) {
          out.push({ targetId: targetId, item: item });
        }
      });
    });
    return out;
  }

  // deps: { revokePreview(previewId) }
  //  - 只撤銷 READY 且未 saved；consumed/saved 不動。
  //  - 撤銷失敗不阻塞（收集後回報，由呼叫端決定是否提示非敏感警告）。
  async function runRevokePending(store, deps) {
    var pending = collectPendingPreviews(store);
    var res = { revoked: 0, failed: [] };
    for (var i = 0; i < pending.length; i++) {
      var item = pending[i].item;
      try {
        await deps.revokePreview(item.preview_id);
        item.status = STATUS.REVOKED;
        res.revoked++;
      } catch (e) {
        res.failed.push(item.preview_id);   // 不阻塞本機 UI 清除
      }
    }
    return res;
  }

  window.CT_PREVIEW_FLOW = {
    STATUS: STATUS,
    stripExt: stripExt,
    previewErrorMessage: previewErrorMessage,
    chooseUploadMode: chooseUploadMode,
    buildPreviewItem: buildPreviewItem,
    getPreviewItems: getPreviewItems,
    addPreviewItem: addPreviewItem,
    hasPreviewItems: hasPreviewItems,
    hasLegacyRecords: hasLegacyRecords,
    classifyTargetSave: classifyTargetSave,
    applyErrorToItem: applyErrorToItem,
    runTempPreviewUpload: runTempPreviewUpload,
    runPreviewSaveForTarget: runPreviewSaveForTarget,
    collectPendingPreviews: collectPendingPreviews,
    runRevokePending: runRevokePending,
  };
})();
