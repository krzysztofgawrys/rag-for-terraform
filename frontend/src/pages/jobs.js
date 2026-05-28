import { apiFetch, esc, toast } from '../api';
let pollTimer = null;
const POLL_INTERVAL = 3000;
let currentOffset = 0;
const PAGE_SIZE = 20;
export function initJobsPage() {
    document.getElementById('indexBtn').addEventListener('click', triggerIndex);
    document.getElementById('refreshJobsBtn').addEventListener('click', () => loadJobs());
    document.getElementById('jobsPrevBtn').addEventListener('click', () => {
        currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
        loadJobs();
    });
    document.getElementById('jobsNextBtn').addEventListener('click', () => {
        currentOffset += PAGE_SIZE;
        loadJobs();
    });
}
export function stopJobsPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}
function startPollingIfNeeded(jobs) {
    stopJobsPolling();
    const hasActive = jobs.some((j) => j.status === 'pending' || j.status === 'running');
    if (hasActive) {
        pollTimer = setInterval(() => loadJobs(true), POLL_INTERVAL);
    }
}
export async function loadJobs(silent = false) {
    try {
        const data = await apiFetch(`/index/?limit=${PAGE_SIZE}&offset=${currentOffset}`);
        const jobs = data.items;
        const tbody = document.getElementById('jobsBody');
        if (!data.total) {
            stopJobsPolling();
            document.getElementById('jobsTotal').textContent = '';
            document.getElementById('jobsPrevBtn').disabled = true;
            document.getElementById('jobsNextBtn').disabled = true;
            if (!silent) {
                tbody.innerHTML =
                    '<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:20px">No jobs yet</td></tr>';
            }
            return;
        }
        document.getElementById('jobsTotal').textContent =
            `${data.total} total - showing ${currentOffset + 1}-${Math.min(currentOffset + PAGE_SIZE, data.total)}`;
        document.getElementById('jobsPrevBtn').disabled = currentOffset === 0;
        document.getElementById('jobsNextBtn').disabled = currentOffset + PAGE_SIZE >= data.total;
        tbody.innerHTML = jobs
            .map((j) => {
            const isActive = j.status === 'pending' || j.status === 'running';
            const modulesCell = j.stats?.modules
                ? `<span style="color:var(--green)">${j.stats.modules}</span> mod / <span style="color:var(--purple)">${j.stats.versions ?? '?'}</span> ver / <span style="color:var(--text3)">${j.stats.added ?? 0}</span> rows`
                : j.stats?.added != null && j.stats.added > 0
                    ? `<span style="color:var(--text3)">${j.stats.added}${j.stats.total ? '/' + j.stats.total : ''}</span> indexed`
                    : (isActive ? '<span style="color:var(--text3)">starting...</span>' : '\u2014');
            return `
      <tr>
        <td>${esc(j.repo)}</td>
        <td>${esc(j.branch || '\u2014')}</td>
        <td class="sha">${j.commit_sha ? j.commit_sha.slice(0, 8) : '\u2014'}</td>
        <td><span class="status-badge ${j.status}">${j.status}${isActive ? '<span class="pulse-dot"></span>' : ''}</span></td>
        <td style="color:var(--text3)">${esc(j.triggered_by || '\u2014')}</td>
        <td style="font-size:11px">${modulesCell}</td>
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
            btn.addEventListener('click', () => triggerReindex(btn.dataset.jobId));
        });
        // Attach delete handlers
        tbody.querySelectorAll('.delete-btn').forEach((btn) => {
            btn.addEventListener('click', () => deleteJob(btn.dataset.jobId));
        });
        // Attach cancel handlers
        tbody.querySelectorAll('.cancel-btn').forEach((btn) => {
            btn.addEventListener('click', () => cancelJob(btn.dataset.jobId));
        });
        startPollingIfNeeded(jobs);
    }
    catch {
        if (!silent) {
            document.getElementById('jobsBody').innerHTML =
                '<tr class="loading-row"><td colspan="8" style="text-align:center">Failed to load jobs</td></tr>';
        }
    }
}
async function triggerReindex(jobId) {
    try {
        const r = await apiFetch(`/index/${jobId}/reindex`, {
            method: 'POST',
        });
        toast('Reindex started: ' + r.id, 'success');
        currentOffset = 0;
        setTimeout(loadJobs, 800);
    }
    catch (e) {
        toast('Reindex failed: ' + e.message, 'error');
    }
}
async function deleteJob(jobId) {
    if (!confirm('Delete this job and all modules it indexed?'))
        return;
    try {
        const r = await apiFetch(`/index/${jobId}`, {
            method: 'DELETE',
        });
        toast(`Deleted job + ${r.modules_deleted} modules`, 'success');
        currentOffset = 0;
        setTimeout(loadJobs, 300);
    }
    catch (e) {
        toast('Delete failed: ' + e.message, 'error');
    }
}
async function cancelJob(jobId) {
    if (!confirm('Stop this indexing job?'))
        return;
    try {
        await apiFetch(`/index/${jobId}/cancel`, { method: 'POST' });
        toast('Job cancelled', 'success');
        currentOffset = 0;
        setTimeout(loadJobs, 300);
    }
    catch (e) {
        toast('Cancel failed: ' + e.message, 'error');
    }
}
async function triggerIndex() {
    const url = document.getElementById('repoUrl').value.trim();
    const branch = document.getElementById('repoBranch').value.trim() || 'main';
    if (!url) {
        toast('Enter a repo URL', 'error');
        return;
    }
    try {
        const discoverTags = document.getElementById('discoverTags').checked;
        const r = await apiFetch('/index/', {
            method: 'POST',
            body: JSON.stringify({ repo_url: url, branch, triggered_by: 'ui', discover_tags: discoverTags }),
        });
        toast('Indexing started: ' + r.id, 'success');
        currentOffset = 0;
        setTimeout(loadJobs, 800);
    }
    catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}
