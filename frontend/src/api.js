const API =
  window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? `${window.location.protocol}//${window.location.hostname}:8000`
    : '';
// --- Auth token management ---------------------------------------------------
let _accessToken = null;
let _authEnabled = false;
export function setAuthEnabled(enabled) {
    _authEnabled = enabled;
}
export function setAccessToken(token) {
    _accessToken = token;
}
export function getAccessToken() {
    return _accessToken;
}
export function isAuthenticated() {
    return _accessToken !== null;
}
export async function apiFetch(path, opts) {
    const headers = { 'Content-Type': 'application/json' };
    if (_accessToken) {
        headers['Authorization'] = `Bearer ${_accessToken}`;
    }
    const fetchOpts = { headers, ...opts };
    if (_authEnabled)
        fetchOpts.credentials = 'include';
    const r = await fetch(API + path, fetchOpts);
    // Try token refresh on 401
    if (r.status === 401 && _accessToken) {
        const refreshed = await tryRefreshToken();
        if (refreshed) {
            // Retry with new token
            headers['Authorization'] = `Bearer ${_accessToken}`;
            const retryOpts = { headers, ...opts };
            if (_authEnabled)
                retryOpts.credentials = 'include';
            const r2 = await fetch(API + path, retryOpts);
            if (!r2.ok)
                throw new Error(`${r2.status} ${r2.statusText}`);
            return r2.json();
        }
        // Refresh failed — redirect to login
        handleAuthFailure();
        throw new Error('Authentication required');
    }
    if (!r.ok)
        throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
}
export async function apiRawFetch(path, opts) {
    const headers = { 'Content-Type': 'application/json' };
    if (_accessToken) {
        headers['Authorization'] = `Bearer ${_accessToken}`;
    }
    const fetchOpts = { headers, ...opts };
    if (_authEnabled)
        fetchOpts.credentials = 'include';
    return fetch(API + path, fetchOpts);
}
async function tryRefreshToken() {
    try {
        const refreshOpts = {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        };
        if (_authEnabled)
            refreshOpts.credentials = 'include';
        const r = await fetch(API + '/auth/refresh-token', refreshOpts);
        if (!r.ok)
            return false;
        const data = await r.json();
        _accessToken = data.access_token;
        return true;
    }
    catch {
        return false;
    }
}
function handleAuthFailure() {
    _accessToken = null;
    // Show login page
    location.hash = '#/login';
}
// --- UI helpers --------------------------------------------------------------
let toastTimer;
export function toast(msg, type = '') {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.className = 'show ' + type;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.className = ''), 3000);
}
export function esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}
