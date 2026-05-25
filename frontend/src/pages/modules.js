import { apiFetch, esc } from '../api';
import { renderDependencyGraph } from './graph';
let allModules = [];
let selectedModule = null;
export async function initModulesPage() {
    document.getElementById('moduleSearch').addEventListener('input', applyFilters);
    const versionSelect = document.getElementById('versionFilter');
    versionSelect.addEventListener('change', () => loadModules());
    const repoSelect = document.getElementById('repoFilter');
    repoSelect.addEventListener('change', () => loadModules());
    await Promise.all([loadVersionOptions(), loadRepoOptions()]);
    await loadModules();
}
async function loadVersionOptions() {
    const select = document.getElementById('versionFilter');
    try {
        const versions = await apiFetch('/modules/versions/all');
        select.innerHTML = '<option value="">all versions</option>';
        for (const v of versions) {
            const opt = document.createElement('option');
            opt.value = v;
            opt.textContent = v;
            select.appendChild(opt);
        }
    }
    catch {
        // keep default
    }
}
async function loadRepoOptions() {
    const select = document.getElementById('repoFilter');
    try {
        const repos = await apiFetch('/modules/repos/all');
        select.innerHTML = '<option value="">all repos</option>';
        for (const r of repos) {
            const opt = document.createElement('option');
            opt.value = r;
            opt.textContent = r;
            select.appendChild(opt);
        }
    }
    catch {
        // keep default
    }
}
async function loadModules() {
    const vFilter = document.getElementById('versionFilter').value;
    const rFilter = document.getElementById('repoFilter').value;
    const vParam = vFilter ? `&version=${encodeURIComponent(vFilter)}` : '';
    const rParam = rFilter ? `&repo=${encodeURIComponent(rFilter)}` : '';
    try {
        // No version param = API returns deduplicated (one per module, most recent)
        // Specific version = API returns only that version's modules
        allModules = await apiFetch(`/modules/?limit=10000${vParam}${rParam}`);
        allModules.sort((a, b) => a.module_name.localeCompare(b.module_name));
        applyFilters();
    }
    catch {
        document.getElementById('moduleList').innerHTML =
            '<div class="empty">Failed to load modules</div>';
    }
}
function applyFilters() {
    const q = document.getElementById('moduleSearch').value.toLowerCase();
    let filtered = allModules;
    if (q) {
        filtered = filtered.filter((m) => m.module_name.toLowerCase().includes(q) ||
            m.module_path.toLowerCase().includes(q) ||
            m.repo.toLowerCase().includes(q) ||
            (m.tags || []).some((t) => t.toLowerCase().includes(q)));
    }
    filtered.sort((a, b) => a.module_name.localeCompare(b.module_name));
    document.getElementById('moduleCount').textContent = String(filtered.length);
    renderModuleList(filtered);
}
function renderModuleList(modules) {
    const el = document.getElementById('moduleList');
    if (!modules.length) {
        el.innerHTML = '<div class="empty">No modules found</div>';
        return;
    }
    el.innerHTML = modules
        .map((m) => `
    <div class="module-item ${selectedModule?.id === m.id ? 'selected' : ''}"
         data-id="${m.id}">
      <div class="module-item-name">
        ${esc(m.module_name)}
      </div>
      <div class="module-item-repo">${esc(m.repo)}</div>
      <div class="module-item-tags">
        ${(m.tags || [])
        .slice(0, 4)
        .map((t) => `<span class="tag">${esc(t)}</span>`)
        .join('')}
        ${(m.tags || []).length > 4 ? `<span class="tag">+${m.tags.length - 4}</span>` : ''}
      </div>
    </div>
  `)
        .join('');
    el.querySelectorAll('.module-item').forEach((item) => {
        item.addEventListener('click', () => {
            const id = item.dataset.id;
            const m = allModules.find((mod) => mod.id === id);
            if (m)
                selectModule(m);
        });
    });
}
// -- Module detail with tabs -------------------------------------------------
async function selectModule(m) {
    selectedModule = m;
    applyFilters();
    const rightPanel = document.querySelector('#page-modules .panel-right');
    rightPanel.innerHTML = `
    <div class="detail-tabs">
      <button class="detail-tab active" data-tab="info">Info</button>
      <button class="detail-tab" data-tab="deps">Dependencies</button>
    </div>
    <div id="tabInfo" class="module-detail"></div>
    <div id="tabDeps" class="module-detail" style="display:none">
      <div id="graphContainer" class="graph-container"></div>
    </div>
  `;
    let graphRendered = false;
    rightPanel.querySelectorAll('.detail-tab').forEach((tab) => {
        tab.addEventListener('click', () => {
            rightPanel.querySelectorAll('.detail-tab').forEach((t) => t.classList.remove('active'));
            tab.classList.add('active');
            const isInfo = tab.dataset.tab === 'info';
            document.getElementById('tabInfo').style.display = isInfo ? 'flex' : 'none';
            document.getElementById('tabDeps').style.display = isInfo ? 'none' : 'flex';
            if (!isInfo && !graphRendered) {
                graphRendered = true;
                renderDependencyGraph(m, navigateToModule);
            }
        });
    });
    await renderInfoTab(m);
}
function navigateToModule(moduleName) {
    const m = allModules.find((mod) => mod.module_name === moduleName);
    if (m)
        selectModule(m);
}
async function renderInfoTab(m) {
    const vars = Object.entries(m.variables || {});
    const outs = Object.entries(m.outputs || {});
    const resources = m.resources || [];
    // Fetch all versions for this module
    let versionsHtml = '';
    try {
        const data = await apiFetch(`/modules/${encodeURIComponent(m.repo)}/${encodeURIComponent(m.module_path)}/versions`);
        versionsHtml = `
      <div>
        <div class="form-label" style="margin-bottom:8px">Versions (${data.versions.length})</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${data.versions
            .map((v) => `<span class="tag ${v.version === m.version ? 'cyan' : ''}" style="cursor:pointer" data-version="${esc(v.version)}">${esc(v.version)}</span>`)
            .join('')}
        </div>
      </div>
    `;
    }
    catch {
        // Ignore version fetch errors
    }
    const infoEl = document.getElementById('tabInfo');
    infoEl.innerHTML = `
    <div class="detail-title">
      ${esc(m.module_name)}
      <span class="repo-badge">${esc(m.repo)}</span>
      <span class="tag" style="color:var(--purple);border-color:var(--purple)">${esc(m.version || '')}</span>
    </div>
    ${m.description ? `<p style="color:var(--text2);font-size:13px;line-height:1.7;font-family:var(--font-sans)">${esc(m.description)}</p>` : ''}
    ${versionsHtml}
    <div>
      <div class="form-label" style="margin-bottom:8px">Tags</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${(m.tags || []).map((t) => `<span class="tag cyan">${esc(t)}</span>`).join('') || '<span style="color:var(--text3);font-size:12px">none</span>'}
      </div>
    </div>
    <div>
      <div class="form-label" style="margin-bottom:8px">Resources</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${resources.map((r) => `<span class="tag orange">${esc(r)}</span>`).join('') || '<span style="color:var(--text3);font-size:12px">none</span>'}
      </div>
    </div>
    <div class="detail-grid">
      <div class="detail-card">
        <div class="detail-card-title">Input Variables (${vars.length})</div>
        ${vars.length
        ? vars
            .map(([n, v]) => `
          <div class="var-row">
            <span class="var-name">${esc(n)}</span>
            <span class="var-type">${esc(String(v.type || 'any'))}</span>
            ${v.required ? '<span class="var-req">required</span>' : ''}
          </div>
        `)
            .join('')
        : '<div style="color:var(--text3);font-size:12px">none</div>'}
      </div>
      <div class="detail-card">
        <div class="detail-card-title">Outputs (${outs.length})</div>
        ${outs.length
        ? outs
            .map(([n, v]) => `
          <div class="var-row">
            <span class="var-name">${esc(n)}</span>
            <span class="var-type" style="flex:1">${esc(v.description || '')}</span>
          </div>
        `)
            .join('')
        : '<div style="color:var(--text3);font-size:12px">none</div>'}
      </div>
    </div>
    <div>
      <div class="form-label" style="margin-bottom:8px">
        Path: <span style="color:var(--purple)">${esc(m.module_path)}</span>
        &nbsp;&middot;&nbsp; Indexed: <span style="color:var(--text3)">${m.indexed_at ? new Date(m.indexed_at).toLocaleString('sv-SE').slice(0, 16) : '\u2014'}</span>
        ${m.commit_sha ? `&nbsp;&middot;&nbsp; <span class="sha">${m.commit_sha.slice(0, 8)}</span>` : ''}
      </div>
    </div>
  `;
    // Version switching — fetch that version's data from API
    infoEl.querySelectorAll('[data-version]').forEach((tag) => {
        tag.addEventListener('click', async () => {
            const ver = tag.dataset.version;
            if (ver === m.version)
                return;
            try {
                const modules = await apiFetch(`/modules/?limit=1&version=${encodeURIComponent(ver)}&repo=${encodeURIComponent(m.repo)}&module_path=${encodeURIComponent(m.module_path)}`);
                const match = modules[0];
                if (match) {
                    selectedModule = match;
                    await renderInfoTab(match);
                }
            }
            catch {
                // Ignore
            }
        });
    });
}
