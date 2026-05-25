import './style.css';
import { apiFetch, toast, setAccessToken, setAuthEnabled } from './api';
import { initRouter, navigateTo } from './router';
import { initModulesPage } from './pages/modules';
import { initQueryPage } from './pages/query';
import { initJobsPage, loadJobs, stopJobsPolling } from './pages/jobs';
import { initUsagePage, loadConsumerJobs, stopUsagePolling } from './pages/usage';
import { initKnowledgePage } from './pages/knowledge';
import { initAuditLogsPage, loadAuditLogs } from './pages/auditlogs';
import { initLoginPage } from './pages/login';
let authMode = 'disabled';
async function loadStats() {
    try {
        const s = await apiFetch('/query/stats');
        document.getElementById('s-modules').textContent = String(s.total_modules ?? '\u2014');
        document.getElementById('s-repos').textContent = String(s.total_repos ?? '\u2014');
        document.getElementById('s-tags').textContent = String(s.unique_tags ?? '\u2014');
        document.getElementById('s-res').textContent = String(s.unique_resource_types ?? '\u2014');
        document.getElementById('s-versions').textContent = String(s.total_versions ?? '\u2014');
        document.getElementById('s-conventions').textContent = String(s.total_conventions ?? '\u2014');
        document.getElementById('s-usages').textContent = String(s.total_usages ?? '\u2014');
    }
    catch (e) {
        console.warn('stats failed', e);
    }
}
function initTheme() {
    const saved = localStorage.getItem('theme');
    const theme = saved === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', theme);
    const icon = document.getElementById('themeIcon');
    icon.innerHTML = theme === 'light' ? '&#x2600;' : '&#x263E;';
    document.getElementById('themeToggle').addEventListener('click', () => {
        const current = document.documentElement.getAttribute('data-theme');
        const next = current === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
        icon.innerHTML = next === 'light' ? '&#x2600;' : '&#x263E;';
    });
}
async function loadUserInfo() {
    if (authMode === 'disabled') {
        document.getElementById('userInfo').style.display = 'none';
        return;
    }
    try {
        const user = await apiFetch('/auth/me');
        const el = document.getElementById('userInfo');
        el.style.display = 'flex';
        document.getElementById('userEmail').textContent = user.email;
        document.getElementById('userRole').textContent = user.role;
    }
    catch {
        document.getElementById('userInfo').style.display = 'none';
    }
}
function setupLogout() {
    document.getElementById('logoutBtn')?.addEventListener('click', async () => {
        try {
            await apiFetch('/auth/logout', { method: 'POST' });
        }
        catch { /* ignore */ }
        setAccessToken(null);
        navigateTo('login');
    });
}
async function enterApp() {
    // Show nav, load data
    await loadUserInfo();
    await loadStats();
    await initModulesPage();
    navigateTo('modules');
}
async function init() {
    initTheme();
    // Determine auth mode from API
    try {
        const info = await apiFetch('/auth/info');
        authMode = info.auth_mode;
    }
    catch {
        // If /auth/info fails, assume disabled (old API without auth)
        authMode = 'disabled';
    }
    setAuthEnabled(authMode !== 'disabled');
    // Check API health
    try {
        await apiFetch('/health');
        document.getElementById('statusDot').title = 'API connected';
    }
    catch {
        const dot = document.getElementById('statusDot');
        dot.style.background = 'var(--red)';
        dot.style.boxShadow = '0 0 12px #f8514933';
        toast('Cannot reach API', 'error');
    }
    // Init all page modules
    initQueryPage();
    initJobsPage();
    initUsagePage();
    initKnowledgePage();
    initAuditLogsPage();
    initLoginPage(() => enterApp());
    setupLogout();
    // Router — lazy load data when navigating to pages, stop polling on leave
    initRouter((pageId) => {
        // Stop polling when leaving those pages
        if (pageId !== 'jobs')
            stopJobsPolling();
        if (pageId !== 'usage')
            stopUsagePolling();
        if (pageId === 'jobs')
            loadJobs();
        if (pageId === 'usage')
            loadConsumerJobs();
        if (pageId === 'knowledge')
            initKnowledgePage();
        if (pageId === 'auditlogs')
            loadAuditLogs();
    });
    if (authMode === 'disabled') {
        // No auth — go straight to app
        await loadStats();
        await initModulesPage();
    }
    else if (authMode === 'sso') {
        // SSO — ALB handles login redirect, we should already have a valid session
        await enterApp();
    }
    else {
        // Local mode — check if we have a valid session, otherwise show login
        try {
            await apiFetch('/auth/me');
            await enterApp();
        }
        catch {
            navigateTo('login');
        }
    }
}
init();
