import { apiFetch, setAccessToken, toast } from '../api';
export function initLoginPage(onLoginSuccess) {
    const form = document.getElementById('loginForm');
    if (!form)
        return;
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const email = document.getElementById('loginEmail').value;
        const password = document.getElementById('loginPassword').value;
        const btn = document.getElementById('loginBtn');
        btn.disabled = true;
        btn.textContent = 'Logging in...';
        try {
            const resp = await apiFetch('/auth/login', {
                method: 'POST',
                body: JSON.stringify({ email, password }),
            });
            setAccessToken(resp.access_token);
            toast('Logged in', 'success');
            onLoginSuccess();
        }
        catch (err) {
            toast('Login failed: ' + err.message, 'error');
        }
        finally {
            btn.disabled = false;
            btn.textContent = 'Login';
        }
    });
}
