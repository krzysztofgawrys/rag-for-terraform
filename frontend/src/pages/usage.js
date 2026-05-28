import { apiFetch, esc, toast } from '../api';
let pollTimer = null;
const POLL_INTERVAL = 3000;
let currentOffset = 0;
const PAGE_SIZE = 20;
export function initUsagePage() {
    document.getElementById('consumerIndexBtn').addEventListener('click', triggerConsumerIndex);
    document.getElementById('refreshConsumerJobsBtn').addEventListener('click', () => loadConsumerJobs());
    document.getElementById('consumerJobsPrevBtn').addEventListener('click', () => {
        currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
        loadConsumerJobs();
    });
    document.getElementById('consumerJobsNextBtn').addEventListener('click', () => {
        currentOffset += PAGE_SIZE;
        loadConsumerJobs();
    });
}
export function stopUsagePolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}
function startPollingIfNeeded(jobs) {
    stopUsagePolling();
    const hasActive = jobs.some((j) => j.status === 'pending' || j.status === 'running');
    if (hasActive) {
        pollTimer = setInterval(() => loadConsumerJobs(true), POLL_INTERVAL);
    }
}
export async function loadConsumerJobs(silent = false) {
    try {
        const data = await apiFetch(`/consumer/?limit=${PAGE_SIZE}&offset=${currentOffset}`);
        const jobs = data.items;
        const tbody = document.getElementById('consumerJobsBody');
        if (!data.total) {
            stopUsagePolling();
            document.getElementById('consumerJobsTotal').textContent = '';
            document.getElementById('consumerJobsPrevBtn').disabled = true;
            document.getElementById('consumerJobsNextBtn').disabled = true;
            if (!silent) {
                tbody.innerHTML =
                    '<tr><td colspan="7" style="text-align:center;color:var(--text3);padding:20px">No consumer jobs yet</td></tr>';
            }
            return;
        }
        document.getElementById('consumerJobsTotal').textContent =
            `${data.total} total - showing ${currentOffset + 1}-${Math.min(currentOffset + PAGE_SIZE, data.total)}`;
        document.getElementById('consumerJobsPrevBtn').disabled = currentOffset === 0;
        document.getElementById('consumerJobsNextBtn').disabled = currentOffset + PAGE_SIZE >= data.total;
        tbody.innerHTML = jobs
            .map((j) => {
            const isActive = j.status === 'pending' || j.status === 'running';
            const usageStats = j.stats?.embedded
                ? `<span style="color:var(--green)">${j.stats.embedded}</span> embedded / <span style="color:var(--text3)">${j.stats.parsed ?? 0}</span> parsed`
                : (j.stats?.parsed ? `${j.stats.parsed} parsed` : '\u2014');
            const d = j.stats?.distillation;
            const distillStats = d
                ? `<span style="color:var(--purple)">${d.modules ?? 0}</span> mod / <span style="color:var(--orange)">${d.dimensions ?? 0}</span> dim`
                    + (d.stale_marked ? ` / <span style="color:var(--red)" title="Conventions marked stale (failed quality gate)">${d.stale_marked} stale</span>` : '')
                    + (d.kept_existing ? ` / <span style="color:var(--text3)" title="Kept existing higher-quality convention">${d.kept_existing} kept</span>` : '')
                : '\u2014';
            return `
      <tr>
        <td>${esc(j.repo)}</td>
        <td>${esc(j.branch || '\u2014')}</td>
        <td><span class="status-badge ${j.status}">${j.status}${isActive ? '<span class="pulse-dot"></span>' : ''}</span></td>
        <td style="font-size:11px">${usageStats}</td>
        <td style="font-size:11px">${distillStats}</td>
        <td style="color:var(--text3);font-size:11px">${j.started_at ? new Date(j.started_at).toLocaleString('sv-SE').slice(0, 16) : '\u2014'}</td>
        <td style="display:flex;gap:4px">
          ${isActive ? `<button class="cancel-btn" data-job-id="${j.id}" style="background:none;border:1px solid var(--red);border-radius:3px;color:var(--red);font:inherit;font-size:10px;padding:2px 6px;cursor:pointer">&#x25A0; Stop</button>` : ''}
          ${j.repo_url && !isActive ? `<button class="reindex-btn" data-job-id="${j.id}">&#x21BB; Reindex</button>` : ''}
          ${!isActive ? `<button class="delete-btn" data-job-id="${j.id}" style="background:none;border:1px solid var(--border);border-radius:3px;color:var(--text3);font:inherit;font-size:10px;padding:2px 6px;cursor:pointer">&#x2715; Delete</button>` : ''}
        </td>
      </tr>
    `;
        })
            .join('');
        // Attach reindex handlers
        tbody.querySelectorAll('.reindex-btn').forEach((btn) => {
            btn.addEventListener('click', () => triggerConsumerReindex(btn.dataset.jobId));
        });
        // Attach delete handlers
        tbody.querySelectorAll('.delete-btn').forEach((btn) => {
            btn.addEventListener('click', () => deleteConsumerJob(btn.dataset.jobId));
        });
        // Attach cancel handlers
        tbody.querySelectorAll('.cancel-btn').forEach((btn) => {
            btn.addEventListener('click', () => cancelConsumerJob(btn.dataset.jobId));
        });
        startPollingIfNeeded(jobs);
    }
    catch {
        if (!silent) {
            document.getElementById('consumerJobsBody').innerHTML =
                '<tr class="loading-row"><td colspan="7" style="text-align:center">Failed to load consumer jobs</td></tr>';
        }
    }
}
async function triggerConsumerIndex() {
    const url = document.getElementById('consumerRepoUrl').value.trim();
    const branch = document.getElementById('consumerRepoBranch').value.trim() || 'main';
    if (!url) {
        toast('Enter a consumer repo URL', 'error');
        return;
    }
    try {
        const runDistillation = document.getElementById('runDistillation').checked;
        const r = await apiFetch('/consumer/', {
            method: 'POST',
            body: JSON.stringify({
                repo_url: url,
                branch,
                triggered_by: 'ui',
                run_distillation: runDistillation,
            }),
        });
        toast('Consumer indexing started: ' + r.id, 'success');
        currentOffset = 0;
        setTimeout(loadConsumerJobs, 800);
    }
    catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}
async function cancelConsumerJob(jobId) {
    if (!confirm('Stop this consumer indexing job?'))
        return;
    try {
        await apiFetch(`/consumer/${jobId}/cancel`, { method: 'POST' });
        toast('Job cancelled', 'success');
        currentOffset = 0;
        setTimeout(() => loadConsumerJobs(), 300);
    }
    catch (e) {
        toast('Cancel failed: ' + e.message, 'error');
    }
}
async function deleteConsumerJob(jobId) {
    if (!confirm('Delete this job and all usage/convention snippets it produced?'))
        return;
    try {
        const r = await apiFetch(`/consumer/${jobId}`, { method: 'DELETE' });
        toast(`Deleted job + ${r.usages_deleted} usages + ${r.conventions_deleted} conventions`, 'success');
        currentOffset = 0;
        setTimeout(() => loadConsumerJobs(), 300);
    }
    catch (e) {
        toast('Delete failed: ' + e.message, 'error');
    }
}
async function triggerConsumerReindex(jobId) {
    try {
        const r = await apiFetch(`/consumer/${jobId}/reindex`, {
            method: 'POST',
        });
        toast('Consumer reindex started: ' + r.id, 'success');
        currentOffset = 0;
        setTimeout(loadConsumerJobs, 800);
    }
    catch (e) {
        toast('Reindex failed: ' + e.message, 'error');
    }
}
