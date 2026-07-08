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

// ---------------------------------------------------------
// Preview Evidence Artifact API client（P9 Phase 2A.1）
// ---------------------------------------------------------
// 對 P9A 後端 preview 端點（backend/app/api/preview.py）的薄封裝。
//
// 設計約束（與規格一致）：
//   - 沿用 classic script + window 全域風格，掛在 window.CT_PREVIEW。
//   - 不新增第二套 token storage：沿用既有 localStorage['ct_token']。
//   - 前端「絕不」保存或暴露 `_records`：每個 response 都走 allowlist
//     _pick()，即使 server 意外回 `_records` 也被濾掉。
//   - 錯誤訊息一律用靜態中文字串，不夾帶 token / header / 原始檔內容。
//   - createPreview 走 multipart：不自行設 Content-Type，交瀏覽器產 boundary。
//
// 匯出：createPreview / getPreview / sealPreview / savePreview /
//       revokePreview / parsePreviewError / sanitizePreviewResponse
// ---------------------------------------------------------
(function () {
  'use strict';

  // response allowlist —— 明確挑選欄位，絕不用 {...data} 全量傳回。
  const CREATE_FIELDS = ['preview_id', 'features', 'total', 'plotted', 'skipped', 'parser_type', 'expires_at'];
  const READ_FIELDS   = ['features', 'total', 'plotted', 'skipped'];
  const SAVE_FIELDS   = ['ok', 'evidence_id', 'sha256_full', 'total', 'inserted', 'skipped'];
  const OK_FIELDS     = ['ok'];

  // 各 error kind 的固定中文訊息（靜態、不含任何敏感資訊）。
  const MSG = {
    auth_required:  '登入狀態已失效，請重新登入。',
    forbidden:      '你沒有權限存取這筆預覽資料。',
    not_found:      '找不到這筆預覽資料。',
    expired:        '預覽已過期，請重新上傳檔案。',
    revoked:        '預覽已撤銷。',
    consumed:       '預覽已完成存證，不可重複使用。',
    too_large:      '檔案超過目前預覽容量，請改用正式上傳。',
    sha_mismatch:   '原始檔完整性驗證失敗，請重新建立預覽。',
    no_key:         '預覽加密服務尚未完成設定，請聯絡系統管理員。',
    server:         '伺服器發生錯誤，請稍後再試。',
    network:        '網路連線失敗，請檢查連線後重試。',
    generic:        '發生未預期的錯誤，請稍後再試。',
    // encrypted_file / diagnosis 刻意留白 → 改用後端 detail（見 _errMessage）。
    encrypted_file: '',
    diagnosis:      '',
  };

  // 依 allowlist 複製欄位；不 mutate 來源、不使用展開全量。
  function _pick(src, allow) {
    const out = {};
    if (!src || typeof src !== 'object') return out;
    for (let i = 0; i < allow.length; i++) {
      const k = allow[i];
      if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
    }
    return out;
  }

  // 沿用既有 JWT 來源；讀不到就回空字串（不拋、不 log）。
  function _getToken() {
    try {
      return (typeof localStorage !== 'undefined' && localStorage.getItem('ct_token')) || '';
    } catch (e) {
      return '';
    }
  }

  // 當前 API base（含 /api）—— 呼叫時解析，方便測試覆寫 window.CT_API。
  function _apiBase() {
    return (typeof window !== 'undefined' && window.CT_API) || '';
  }

  // 統一的 PreviewError 建構（instanceof Error，但帶 kind/status/code/request_id/diagnosis）。
  function makePreviewError(kind, status, message, diagnosis) {
    const msg = message || MSG[kind] || MSG.generic;
    const err = new Error(msg);
    err.name = 'PreviewError';
    err.kind = kind;
    err.status = status || 0;
    err.message = msg;
    err.code = null;         // machine-readable code（parsePreviewError 會覆寫）
    err.request_id = null;   // 錯誤追蹤碼（server 回傳時覆寫）
    if (diagnosis !== undefined) err.diagnosis = diagnosis;
    return err;
  }

  function _errMessage(kind, detail) {
    // encrypted_file / diagnosis 用後端訊息（若有），其餘用固定文案。
    if (MSG[kind]) return MSG[kind];
    return detail || MSG.generic;
  }

  // machine-readable error code → 前端 kind（P9 Phase 2A.3）。
  // PREVIEW_PARSE_FAILED 需看 diagnosis/status 進一步判別，故不放這張表（見 _kindFromCode）。
  const CODE_TO_KIND = {
    PREVIEW_EXPIRED:  'expired',
    PREVIEW_REVOKED:  'revoked',
    PREVIEW_CONSUMED: 'consumed',
    PREVIEW_TOO_LARGE: 'too_large',
    PREVIEW_STORAGE_UNAVAILABLE: 'too_large',
    PREVIEW_KEY_MISSING: 'no_key',
    PREVIEW_SHA_MISMATCH: 'sha_mismatch',
    PREVIEW_FORBIDDEN: 'forbidden',
    PREVIEW_NOT_FOUND: 'not_found',
    AUTH_REQUIRED: 'auth_required',
    INTERNAL_ERROR: 'server',
  };

  function _kindFromCode(code, status, detail, diagnosis) {
    if (code === 'PREVIEW_PARSE_FAILED') {
      if (diagnosis !== undefined && diagnosis !== null) return 'diagnosis';
      if (detail.indexOf('密碼') !== -1 || detail.indexOf('加密') !== -1) return 'encrypted_file';
      return status >= 500 ? 'server' : 'generic';
    }
    return CODE_TO_KIND[code] || null;
  }

  // legacy fallback：無 machine-readable code 時，靠 status + 中文 detail 判定。
  // TODO：所有 production instance 都完成新 error contract 後移除 legacy detail fallback。
  function _kindByStatus(status, detail, diagnosis) {
    if (status === 401) return 'auth_required';
    if (status === 403) return 'forbidden';
    if (status === 404) return 'not_found';
    if (status === 409) return 'sha_mismatch';
    if (status === 410) {
      if (detail.indexOf('過期') !== -1) return 'expired';
      if (detail.indexOf('撤銷') !== -1) return 'revoked';
      if (detail.indexOf('存證') !== -1 || detail.indexOf('不可重複') !== -1) return 'consumed';
      return 'generic';
    }
    if (status === 413) return 'too_large';
    if (status === 422) {
      if (diagnosis !== undefined && diagnosis !== null) return 'diagnosis';
      if (detail.indexOf('密碼') !== -1 || detail.indexOf('加密') !== -1) return 'encrypted_file';
      return 'generic';
    }
    if (status === 503) return /金鑰|key|加密服務|加密金鑰|未設定|config/i.test(detail) ? 'no_key' : 'server';
    if (status >= 500) return 'server';
    return 'generic';
  }

  // 依 machine-readable error contract 優先判定 kind；缺 code 時退回 legacy status/detail。
  // 回傳的 PreviewError 帶 code + request_id（追蹤碼）。
  function parsePreviewError(status, body) {
    body = body || {};
    // 新 contract 的 error 必為物件 {code,message,details}；字串型 error（legacy）不算。
    const contractErr = (body.error && typeof body.error === 'object') ? body.error : null;
    const requestId = (typeof body.request_id === 'string') ? body.request_id : null;
    const code = contractErr && contractErr.code ? contractErr.code : null;
    const codeMsg = contractErr && typeof contractErr.message === 'string' ? contractErr.message : '';
    const codeDetails = (contractErr && contractErr.details) || null;

    // detail / diagnosis：新 contract 優先，legacy 為備援。
    const detail = codeMsg || ((typeof body.detail === 'string') ? body.detail : '');
    const diagnosis = (codeDetails && codeDetails.diagnosis) || body.diagnosis;

    let kind = null;
    if (code) kind = _kindFromCode(code, status, detail, diagnosis);
    if (!kind) kind = _kindByStatus(status, detail, diagnosis);   // legacy fallback

    const err = makePreviewError(kind, status, _errMessage(kind, detail));
    err.code = code;
    err.request_id = requestId;
    if (kind === 'diagnosis') err.diagnosis = diagnosis;
    return err;
  }

  // 共用 fetch：加 base + Authorization、解析 JSON、非 2xx → parsePreviewError、
  // 網路錯誤 → network。任何路徑都不外洩 token / header / 原始檔內容。
  async function previewFetch(path, options) {
    options = options || {};
    const token = _getToken();
    if (options.auth !== false && !token) {
      // 不發出帶 Bearer null/undefined 的 request。
      throw makePreviewError('auth_required', 0);
    }

    const headers = Object.assign({}, options.headers || {});
    if (token) headers['Authorization'] = 'Bearer ' + token;

    let res;
    try {
      res = await fetch(_apiBase() + path, {
        method: options.method || 'GET',
        headers: headers,
        body: options.body,
      });
    } catch (e) {
      // 刻意不夾帶 e.message（可能含 URL）——用固定文案。
      throw makePreviewError('network', 0);
    }

    let text = '';
    try { text = await res.text(); } catch (e) { text = ''; }
    let json = null;
    if (text) { try { json = JSON.parse(text); } catch (e) { json = null; } }

    if (!res.ok) {
      throw parsePreviewError(res.status, json);
    }
    return json || {};
  }

  // ── API functions ──────────────────────────────────────────

  // POST /api/preview（multipart）→ 只回 allowlist 欄位（不含 _records）。
  async function createPreview(file, targetId) {
    const fd = new FormData();
    fd.append('file', file);
    // target_id 有值才送（trim 後非空）；空值交後端以檔名推導。
    if (targetId !== undefined && targetId !== null && String(targetId).trim() !== '') {
      fd.append('target_id', String(targetId));
    }
    // 不設 Content-Type：讓瀏覽器為 multipart 產生 boundary。
    const body = await previewFetch('/preview', { method: 'POST', body: fd });
    return _pick(body, CREATE_FIELDS);
  }

  // GET /api/preview/{id} → 唯讀重建，只回 allowlist 欄位。
  async function getPreview(previewId) {
    const body = await previewFetch('/preview/' + encodeURIComponent(previewId), { method: 'GET' });
    return _pick(body, READ_FIELDS);
  }

  // POST /api/preview/{id}/seal → { ok:true }。
  async function sealPreview(previewId) {
    const body = await previewFetch('/preview/' + encodeURIComponent(previewId) + '/seal', { method: 'POST' });
    return _pick(body, OK_FIELDS);
  }

  // POST /api/preview/{id}/save → server 端存證（不要求前置 seal，後端會 inline seal）。
  // body 只送 project_id / target_id；不回送任何 records。
  async function savePreview(previewId, projectId, targetId) {
    const payload = {
      project_id: projectId,
      target_id: (targetId === undefined || targetId === null) ? '' : targetId,
    };
    const body = await previewFetch('/preview/' + encodeURIComponent(previewId) + '/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return _pick(body, SAVE_FIELDS);
  }

  // DELETE /api/preview/{id} → 撤銷。
  async function revokePreview(previewId) {
    const body = await previewFetch('/preview/' + encodeURIComponent(previewId), { method: 'DELETE' });
    return _pick(body, OK_FIELDS);
  }

  // 純函式：依 kind allowlist 過濾任意 response（不 mutate 來源）。
  function sanitizePreviewResponse(data, kind) {
    const map = { create: CREATE_FIELDS, read: READ_FIELDS, save: SAVE_FIELDS, seal: OK_FIELDS, revoke: OK_FIELDS };
    return _pick(data, map[kind] || READ_FIELDS);
  }

  window.CT_PREVIEW = {
    createPreview: createPreview,
    getPreview: getPreview,
    sealPreview: sealPreview,
    savePreview: savePreview,
    revokePreview: revokePreview,
    parsePreviewError: parsePreviewError,
    sanitizePreviewResponse: sanitizePreviewResponse,
  };
})();
