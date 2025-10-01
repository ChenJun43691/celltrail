// frontend/api.js
const API = 'https://celltrail-api.onrender.com';

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