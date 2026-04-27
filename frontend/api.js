// frontend/api.js
// ---------------------------------------------------------
// API base 依環境自動切換：
//   1. window.__CELLTRAIL_API__（測試 / staging 可由 index.html 覆寫）
//   2. 本機 hostname（localhost / 127.0.0.1 / 區網）→ http://localhost:8000
//   3. 其他 → 正式環境 Render URL
// ---------------------------------------------------------
function resolveApiBase() {
  if (typeof window !== 'undefined' && window.__CELLTRAIL_API__) {
    return window.__CELLTRAIL_API__.replace(/\/$/, '');
  }
  const host = (typeof location !== 'undefined' && location.hostname) || '';
  const isLocal = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|192\.168\.|10\.)/.test(host);
  if (isLocal) return 'http://localhost:8000';
  return 'https://celltrail-api.onrender.com';
}

const API = resolveApiBase();

export async function login(username, password) {
  const body = new URLSearchParams({ username, password }); // x-www-form-urlencoded
  const res = await fetch(`${API}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`login failed: ${res.status} ${text}`);
  }
  const data = await res.json(); // { access_token, token_type }
  localStorage.setItem('token', data.access_token);
  return data;
}

export async function me() {
  const token = localStorage.getItem('token');
  if (!token) throw new Error('no token, please login');
  const res = await fetch(`${API}/api/auth/me`, {
    headers: { Authorization: `Bearer ${token}` }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`auth failed: ${res.status} ${text}`);
  }
  return res.json();
}

export function logout() {
  localStorage.removeItem('token');
}

export { API };
