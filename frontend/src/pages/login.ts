import { apiFetch, setAccessToken, setUserRole, toast } from '../api';
import type { TokenResponse } from '../types';

/** Decode JWT payload without verification (role is enforced server-side). */
function parseJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const payload = atob(parts[1].replace(/-/g, '+').replace(/_/g, '/'));
    return JSON.parse(payload);
  } catch {
    return null;
  }
}

export function initLoginPage(onLoginSuccess: () => void): void {
  const form = document.getElementById('loginForm') as HTMLFormElement | null;
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = (document.getElementById('loginEmail') as HTMLInputElement).value;
    const password = (document.getElementById('loginPassword') as HTMLInputElement).value;
    const btn = document.getElementById('loginBtn') as HTMLButtonElement;

    btn.disabled = true;
    btn.textContent = 'Logging in...';

    try {
      const resp = await apiFetch<TokenResponse>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email, password }),
      });
      setAccessToken(resp.access_token);

      // Extract role from JWT immediately so applyRoleVisibility works
      // even if the subsequent /auth/me call is slow or rate-limited.
      const claims = parseJwtPayload(resp.access_token);
      if (claims && typeof claims.role === 'string') {
        setUserRole(claims.role);
      }

      toast('Logged in', 'success');
      onLoginSuccess();
    } catch (err) {
      toast('Login failed: ' + (err as Error).message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Login';
    }
  });
}
