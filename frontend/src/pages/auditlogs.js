import { apiFetch, esc } from '../api';
let currentOffset = 0;
const PAGE_SIZE = 50;
export function initAuditLogsPage() {
    document.getElementById('refreshAuditBtn').addEventListener('click', () => {
        currentOffset = 0;
        loadAuditLogs();
    });
    document.getElementById('auditCategoryFilter').addEventListener('change', () => {
        currentOffset = 0;
        loadAuditLogs();
    });
    document.getElementById('auditStatusFilter').addEventListener('change', () => {
        currentOffset = 0;
        loadAuditLogs();
    });
    document.getElementById('auditPrevBtn').addEventListener('click', () => {
        currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
        loadAuditLogs();
    });
    document.getElementById('auditNextBtn').addEventListener('click', () => {
        currentOffset += PAGE_SIZE;
        loadAuditLogs();
    });
    document.getElementById('auditDetailClose').addEventListener('click', () => {
        document.getElementById('auditDetail').style.display = 'none';
    });
}
export async function loadAuditLogs() {
    const tbody = document.getElementById('auditLogsBody');
    const category = document.getElementById('auditCategoryFilter').value;
    const status = document.getElementById('auditStatusFilter').value;
    let qs = `?limit=${PAGE_SIZE}&offset=${currentOffset}`;
    if (category)
        qs += `&category=${category}`;
    if (status)
        qs += `&status=${status}`;
    try {
        const data = await apiFetch(`/audit/${qs}`);
        // Update pagination
        document.getElementById('auditTotal').textContent =
            `${data.total} total \u2022 showing ${currentOffset + 1}\u2013${Math.min(currentOffset + PAGE_SIZE, data.total)}`;
        document.getElementById('auditPrevBtn').disabled = currentOffset === 0;
        document.getElementById('auditNextBtn').disabled =
            currentOffset + PAGE_SIZE >= data.total;
        if (!data.items.length) {
            tbody.innerHTML =
                '<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:20px">No audit logs</td></tr>';
            return;
        }
        tbody.innerHTML = data.items
            .map((log) => `
      <tr class="audit-row" data-id="${log.id}" style="cursor:pointer">
        <td style="color:var(--text3);font-size:11px;white-space:nowrap">${formatDate(log.created_at)}</td>
        <td><span class="audit-category-badge ${log.category}">${log.category}</span></td>
        <td style="font-size:11px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(log.action)}</td>
        <td><span class="status-badge ${log.status}">${log.status}</span></td>
        <td style="color:var(--text3);font-size:11px;text-align:right">${log.duration_ms != null ? log.duration_ms + 'ms' : '\u2014'}</td>
        <td style="color:var(--text3);font-size:10px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${summarize(log)}</td>
      </tr>
    `)
            .join('');
        // Click to expand detail
        tbody.querySelectorAll('.audit-row').forEach((row) => {
            row.addEventListener('click', () => {
                const id = row.dataset.id;
                const log = data.items.find((l) => l.id === id);
                if (log)
                    showDetail(log);
            });
        });
    }
    catch {
        tbody.innerHTML =
            '<tr><td colspan="6" style="text-align:center;color:var(--red);padding:20px">Failed to load audit logs</td></tr>';
    }
}
function showDetail(log) {
    const panel = document.getElementById('auditDetail');
    const content = document.getElementById('auditDetailContent');
    const sections = [
        `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
      <div><span class="form-label">Category</span><div><span class="audit-category-badge ${log.category}">${log.category}</span></div></div>
      <div><span class="form-label">Action</span><div style="color:var(--text)">${esc(log.action)}</div></div>
      <div><span class="form-label">Status</span><div><span class="status-badge ${log.status}">${log.status}</span></div></div>
      <div><span class="form-label">Duration</span><div style="color:var(--cyan)">${log.duration_ms != null ? log.duration_ms + 'ms' : '\u2014'}</div></div>
    </div>`,
        `<div style="margin-bottom:4px"><span class="form-label">Timestamp</span></div>
     <div style="color:var(--text3);margin-bottom:12px">${log.created_at}</div>`,
    ];
    if (log.error) {
        sections.push(`<div style="margin-bottom:4px"><span class="form-label">Error</span></div>
       <div class="code-block" style="white-space:pre-wrap;word-break:break-word;color:var(--red);max-height:200px">${esc(log.error)}</div>`);
    }
    if (log.request_data != null) {
        sections.push(`<div style="margin-bottom:4px"><span class="form-label">Request / Prompt</span></div>
       <div class="code-block" style="white-space:pre-wrap;word-break:break-word;max-height:300px">${esc(formatJson(log.request_data))}</div>`);
    }
    if (log.response_data != null) {
        sections.push(`<div style="margin-bottom:4px"><span class="form-label">Response</span></div>
       <div class="code-block" style="white-space:pre-wrap;word-break:break-word;max-height:300px">${esc(formatJson(log.response_data))}</div>`);
    }
    if (log.metadata && Object.keys(log.metadata).length) {
        sections.push(`<div style="margin-bottom:4px"><span class="form-label">Metadata</span></div>
       <div class="code-block" style="white-space:pre-wrap;word-break:break-word;max-height:200px">${esc(formatJson(log.metadata))}</div>`);
    }
    content.innerHTML = sections.join('');
    panel.style.display = 'block';
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function formatDate(iso) {
    if (!iso)
        return '\u2014';
    return new Date(iso).toLocaleString('sv-SE').slice(0, 19);
}
function formatJson(obj) {
    if (typeof obj === 'string')
        return obj;
    try {
        return JSON.stringify(obj, null, 2);
    }
    catch {
        return String(obj);
    }
}
function summarize(log) {
    if (log.error)
        return log.error.slice(0, 60);
    if (log.category === 'llm') {
        const req = log.request_data;
        if (req?.prompt) {
            const prompt = String(req.prompt);
            return prompt.slice(0, 60) + (prompt.length > 60 ? '\u2026' : '');
        }
    }
    if (log.category === 'api') {
        const resp = log.response_data;
        if (resp?.status_code)
            return `HTTP ${resp.status_code}`;
    }
    if (log.category === 'mcp') {
        const resp = log.response_data;
        if (resp?.response_length)
            return `${resp.response_length} chars`;
    }
    if (log.category === 'worker') {
        const req = log.request_data;
        if (req?.repo_url)
            return String(req.repo_url).split('/').pop() || '';
    }
    return '\u2014';
}
