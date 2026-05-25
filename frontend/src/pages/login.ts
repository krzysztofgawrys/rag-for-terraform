import { apiFetch, setAccessToken, toast } from '../api';
import type { TokenResponse } from '../types';

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
