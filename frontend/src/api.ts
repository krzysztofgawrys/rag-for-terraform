const API =
  window.location.port === '3000'
    ? `${window.location.protocol}//${window.location.hostname}:8000`
    : '';

// --- Auth token management ---------------------------------------------------

let _accessToken: string | null = null;
let _authEnabled = false;

export function setAuthEnabled(enabled: boolean): void {
  _authEnabled = enabled;
}

export function setAccessToken(token: string | null): void {
  _accessToken = token;
}

export function getAccessToken(): string | null {
  return _accessToken;
}

export function isAuthenticated(): boolean {
  return _accessToken !== null;
}

export async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (_accessToken) {
    headers['Authorization'] = `Bearer ${_accessToken}`;
  }

  const fetchOpts: RequestInit = { headers, ...opts };
  if (_authEnabled) fetchOpts.credentials = 'include';

  const r = await fetch(API + path, fetchOpts);

  // Try token refresh on 401
  if (r.status === 401 && _accessToken) {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      // Retry with new token
      headers['Authorization'] = `Bearer ${_accessToken}`;
      const retryOpts: RequestInit = { headers, ...opts };
      if (_authEnabled) retryOpts.credentials = 'include';
      const r2 = await fetch(API + path, retryOpts);
      if (!r2.ok) throw new Error(`${r2.status} ${r2.statusText}`);
      return r2.json() as Promise<T>;
    }
    // Refresh failed — redirect to login
    handleAuthFailure();
    throw new Error('Authentication required');
  }

  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export async function apiRawFetch(path: string, opts?: RequestInit): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (_accessToken) {
    headers['Authorization'] = `Bearer ${_accessToken}`;
  }
  const fetchOpts: RequestInit = { headers, ...opts };
  if (_authEnabled) fetchOpts.credentials = 'include';
  return fetch(API + path, fetchOpts);
}

async function tryRefreshToken(): Promise<boolean> {
  try {
    const refreshOpts: RequestInit = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    };
    if (_authEnabled) refreshOpts.credentials = 'include';
    const r = await fetch(API + '/auth/refresh-token', refreshOpts);
    if (!r.ok) return false;
    const data = await r.json();
    _accessToken = data.access_token;
    return true;
  } catch {
    return false;
  }
}

function handleAuthFailure(): void {
  _accessToken = null;
  // Show login page
  location.hash = '#/login';
}

// --- UI helpers --------------------------------------------------------------

let toastTimer: ReturnType<typeof setTimeout>;

export function toast(msg: string, type: 'success' | 'error' | '' = ''): void {
  const el = document.getElementById('toast')!;
  el.textContent = msg;
  el.className = 'show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ''), 3000);
}

export function esc(s: unknown): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
