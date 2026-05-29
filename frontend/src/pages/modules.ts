import { apiFetch, esc } from '../api';
import { renderDependencyGraph } from './graph';
import type { Module } from '../types';

let allModules: Module[] = [];
let selectedModule: Module | null = null;

/** Categorise license for CSS class: permissive, copyleft, bsl, unknown. */
function licenseCat(license: string | undefined): string {
  if (!license) return 'unknown';
  const l = license.toLowerCase();
  if (['mit', 'apache-2.0', 'bsd-2-clause', 'bsd-3-clause', 'isc', 'unlicense'].includes(l)) return 'permissive';
  if (l.startsWith('gpl') || l.startsWith('lgpl') || l === 'mpl-2.0') return 'copyleft';
  if (l.startsWith('bsl')) return 'bsl';
  if (l === 'other') return 'other';
  return 'unknown';
}
let sessionDepth = 1;
let fullModuleCache: Module[] | null = null;

// Dependency graph navigation history (back/forward)
interface NavEntry { repo: string; path: string; version?: string }
const navHistory: NavEntry[] = [];
let navIndex = -1;
let navInProgress = false; // prevent pushing during back/forward

export async function initModulesPage(): Promise<void> {
  document.getElementById('moduleSearch')!.addEventListener('input', applyFilters);

  const versionSelect = document.getElementById('versionFilter') as HTMLSelectElement;
  versionSelect.addEventListener('change', () => loadModules());

  const repoSelect = document.getElementById('repoFilter') as HTMLSelectElement;
  repoSelect.addEventListener('change', () => loadModules());

  // Graph navigation via browser back/forward (mouse buttons, keyboard, gestures).
  // Each graph navigation pushes a history entry. popstate replays from navHistory.
  window.addEventListener('popstate', (e) => {
    const state = e.state;
    if (state?.graphNav != null && state.graphNav < navHistory.length) {
      navIndex = state.graphNav;
      navInProgress = true;
      const entry = navHistory[navIndex];
      navigateToModule(entry.repo, entry.path, entry.version).then(() => {
        navInProgress = false;
      });
    }
  });

  await Promise.all([loadVersionOptions(), loadRepoOptions()]);
  await loadModules();
}

async function loadVersionOptions(): Promise<void> {
  const select = document.getElementById('versionFilter') as HTMLSelectElement;
  try {
    const versions = await apiFetch<string[]>('/modules/versions/all');
    select.innerHTML = '<option value="">all versions</option>';
    for (const v of versions) {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    }
  } catch {
    // keep default
  }
}

async function loadRepoOptions(): Promise<void> {
  const select = document.getElementById('repoFilter') as HTMLSelectElement;
  try {
    const repos = await apiFetch<string[]>('/modules/repos/all');
    select.innerHTML = '<option value="">all repos</option>';
    for (const r of repos) {
      const opt = document.createElement('option');
      opt.value = r;
      opt.textContent = r;
      select.appendChild(opt);
    }
  } catch {
    // keep default
  }
}

async function loadModules(): Promise<void> {
  const vFilter = (document.getElementById('versionFilter') as HTMLSelectElement).value;
  const rFilter = (document.getElementById('repoFilter') as HTMLSelectElement).value;
  const vParam = vFilter ? `&version=${encodeURIComponent(vFilter)}` : '';
  const rParam = rFilter ? `&repo=${encodeURIComponent(rFilter)}` : '';
  try {
    // No version param = API returns deduplicated (one per module, most recent)
    // Specific version = API returns only that version's modules
    allModules = await apiFetch<Module[]>(`/modules/?limit=10000${vParam}${rParam}`);
    allModules.sort((a, b) => a.module_name.localeCompare(b.module_name));
    applyFilters();
  } catch {
    document.getElementById('moduleList')!.innerHTML =
      '<div class="empty">Failed to load modules</div>';
  }
}

function applyFilters(): void {
  const q = (document.getElementById('moduleSearch') as HTMLInputElement).value.toLowerCase();

  let filtered = allModules;

  if (q) {
    filtered = filtered.filter(
      (m) =>
        m.module_name.toLowerCase().includes(q) ||
        m.module_path.toLowerCase().includes(q) ||
        m.repo.toLowerCase().includes(q) ||
        (m.tags || []).some((t) => t.toLowerCase().includes(q)),
    );
  }

  filtered.sort((a, b) => a.module_name.localeCompare(b.module_name));
  document.getElementById('moduleCount')!.textContent = String(filtered.length);
  renderModuleList(filtered);
}

function renderModuleList(modules: Module[]): void {
  const el = document.getElementById('moduleList')!;
  if (!modules.length) {
    el.innerHTML = '<div class="empty">No modules found</div>';
    return;
  }
  el.innerHTML = modules
    .map(
      (m) => `
    <div class="module-item ${selectedModule?.id === m.id ? 'selected' : ''}"
         data-id="${m.id}">
      <div class="module-item-name">
        ${esc(m.module_name)}
      </div>
      <div class="module-item-repo">
        ${esc(m.repo)}
        <span class="license-badge license-${licenseCat(m.license)}">${esc(m.license || 'Unknown')}</span>
      </div>
      <div class="module-item-tags">
        ${(m.tags || [])
          .slice(0, 4)
          .map((t) => `<span class="tag">${esc(t)}</span>`)
          .join('')}
        ${(m.tags || []).length > 4 ? `<span class="tag">+${m.tags!.length - 4}</span>` : ''}
      </div>
    </div>
  `,
    )
    .join('');

  el.querySelectorAll<HTMLElement>('.module-item').forEach((item) => {
    item.addEventListener('click', () => {
      const id = item.dataset.id!;
      const m = allModules.find((mod) => mod.id === id);
      if (m) selectModule(m);
    });
  });
}

// -- Module detail with tabs -------------------------------------------------

async function selectModule(m: Module, initialTab: 'info' | 'deps' = 'info'): Promise<void> {
  selectedModule = m;
  applyFilters();

  // Push to browser history for back/forward navigation
  if (!navInProgress) {
    navHistory.length = navIndex + 1;
    navHistory.push({ repo: m.repo, path: m.module_path, version: m.version });
    navIndex = navHistory.length - 1;
    window.history.pushState({ graphNav: navIndex }, '');
  }

  const rightPanel = document.querySelector<HTMLElement>('#page-modules .panel-right')!;

  const infoActive = initialTab === 'info';
  rightPanel.innerHTML = `
    <div class="detail-tabs">
      <button class="detail-tab ${infoActive ? 'active' : ''}" data-tab="info">Info</button>
      <button class="detail-tab ${infoActive ? '' : 'active'}" data-tab="deps">Dependencies</button>
    </div>
    <div id="tabInfo" class="module-detail" style="display:${infoActive ? 'flex' : 'none'}"></div>
    <div id="tabDeps" class="module-detail" style="display:${infoActive ? 'none' : 'flex'}">
      <div class="graph-toolbar">
        <label class="depth-toggle">
          <span class="depth-label">Version:</span>
          <div id="graphVersionBtns" class="version-btn-group"><span class="depth-label">loading...</span></div>
        </label>
        <label class="depth-toggle">
          <span class="depth-label">Depth:</span>
          <button class="depth-btn${sessionDepth === 1 ? ' active' : ''}" data-depth="1">Direct</button>
          <button class="depth-btn${sessionDepth > 1 ? ' active' : ''}" data-depth="20">Full chain</button>
        </label>
      </div>
      <div id="graphContainer" class="graph-container"></div>
    </div>
  `;

  let graphRendered = false;
  // Use explicit version when: replaying from history, or navigating from dependency graph
  const explicitVersion = (navInProgress || initialTab === 'deps') && m.version ? m.version : '';
  let currentVersion = '';

  async function ensureFullCache(): Promise<void> {
    if (!fullModuleCache) {
      fullModuleCache = await apiFetch<Module[]>('/modules/?limit=10000');
    }
  }

  async function renderGraph() {
    graphRendered = true;
    const modWithVersion = { ...m, version: currentVersion || undefined };
    const lookup = sessionDepth > 1
      ? (repo: string, path: string) =>
          (fullModuleCache || allModules).find((mod) => mod.repo === repo && mod.module_path === path)
      : undefined;
    if (sessionDepth > 1) await ensureFullCache();
    renderDependencyGraph(modWithVersion, navigateToModule, sessionDepth, lookup);
  }

  // Load version buttons
  const vBtnGroup = document.getElementById('graphVersionBtns')!;
  try {
    const data = await apiFetch<{ versions: { version: string }[] }>(
      `/modules/${encodeURIComponent(m.repo)}/${encodeURIComponent(m.module_path)}/versions`,
    );
    vBtnGroup.innerHTML = '';
    // Use explicit version from dependency edge, or pick latest semver tag
    if (explicitVersion && data.versions.some((v) => v.version === explicitVersion)) {
      currentVersion = explicitVersion;
    } else if (data.versions.length) {
      const tagged = data.versions.find((v) => /\d+\.\d+/.test(v.version));
      currentVersion = tagged ? tagged.version : data.versions[0].version;
    }
    for (const v of data.versions) {
      const btn = document.createElement('button');
      btn.className = `depth-btn${v.version === currentVersion ? ' active' : ''}`;
      btn.textContent = v.version;
      btn.addEventListener('click', () => {
        vBtnGroup.querySelectorAll('.depth-btn').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        currentVersion = v.version;
        syncVersionToHistory();
        renderGraph();
      });
      vBtnGroup.appendChild(btn);
    }
  } catch {
    vBtnGroup.innerHTML = '<span class="depth-label">no versions</span>';
  }

  // Always keep history entry in sync with resolved version
  function syncVersionToHistory() {
    if (navIndex >= 0 && currentVersion) {
      navHistory[navIndex].version = currentVersion;
    }
  }
  syncVersionToHistory();

  // Tab switching
  rightPanel.querySelectorAll<HTMLButtonElement>('.detail-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      rightPanel.querySelectorAll('.detail-tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      const isInfo = tab.dataset.tab === 'info';
      document.getElementById('tabInfo')!.style.display = isInfo ? 'flex' : 'none';
      document.getElementById('tabDeps')!.style.display = isInfo ? 'none' : 'flex';

      if (!isInfo && !graphRendered) renderGraph();
    });
  });

  // Depth toggle — persists across module selections within session
  rightPanel.querySelectorAll<HTMLButtonElement>('.depth-btn[data-depth]').forEach((btn) => {
    btn.addEventListener('click', () => {
      rightPanel.querySelectorAll('.depth-btn[data-depth]').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      sessionDepth = parseInt(btn.dataset.depth || '1', 10);
      renderGraph();
    });
  });

  // Render graph immediately if deps tab is active
  if (!infoActive) renderGraph();

  // Render info with the resolved version (latest semver tag)
  await renderInfoTab(currentVersion ? { ...m, version: currentVersion } : m);
}

async function navigateToModule(repo: string, path: string, version?: string): Promise<void> {
  let m = allModules.find((mod) => mod.repo === repo && mod.module_path === path);
  if (!m) {
    (document.getElementById('repoFilter') as HTMLSelectElement).value = '';
    (document.getElementById('versionFilter') as HTMLSelectElement).value = '';
    (document.getElementById('moduleSearch') as HTMLInputElement).value = '';
    await loadModules();
    m = allModules.find((mod) => mod.repo === repo && mod.module_path === path);
  }
  if (m) {
    const withVersion = version ? { ...m, version } : m;
    selectModule(withVersion, 'deps');
  } else {
    const { toast } = await import('../api');
    toast(`Module ${repo}/${path} is not indexed`, 'error');
  }
}


async function renderInfoTab(m: Module): Promise<void> {
  const vars = Object.entries(m.variables || {});
  const outs = Object.entries(m.outputs || {});
  const resources = m.resources || [];

  // Fetch all versions for this module
  let versionsHtml = '';
  try {
    const data = await apiFetch<{ versions: { version: string; indexed_at: string }[] }>(
      `/modules/${encodeURIComponent(m.repo)}/${encodeURIComponent(m.module_path)}/versions`,
    );
    versionsHtml = `
      <div>
        <div class="form-label" style="margin-bottom:8px">Versions (${data.versions.length})</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${data.versions
            .map(
              (v) =>
                `<span class="tag ${v.version === m.version ? 'cyan' : ''}" style="cursor:pointer" data-version="${esc(v.version)}">${esc(v.version)}</span>`,
            )
            .join('')}
        </div>
      </div>
    `;
  } catch {
    // Ignore version fetch errors
  }

  const infoEl = document.getElementById('tabInfo')!;
  infoEl.innerHTML = `
    <div class="detail-title">
      ${esc(m.module_name)}
      <span class="repo-badge">${esc(m.repo)}</span>
      <span class="tag" style="color:var(--purple);border-color:var(--purple)">${esc(m.version || '')}</span>
      <span class="license-badge license-${licenseCat(m.license)}">${esc(m.license || 'Unknown')}</span>
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
        ${
          vars.length
            ? vars
                .map(
                  ([n, v]) => `
          <div class="var-row">
            <span class="var-name">${esc(n)}</span>
            <span class="var-type">${esc(String(v.type || 'any'))}</span>
            ${v.required ? '<span class="var-req">required</span>' : ''}
          </div>
        `,
                )
                .join('')
            : '<div style="color:var(--text3);font-size:12px">none</div>'
        }
      </div>
      <div class="detail-card">
        <div class="detail-card-title">Outputs (${outs.length})</div>
        ${
          outs.length
            ? outs
                .map(
                  ([n, v]) => `
          <div class="var-row">
            <span class="var-name">${esc(n)}</span>
            <span class="var-type" style="flex:1">${esc(v.description || '')}</span>
          </div>
        `,
                )
                .join('')
            : '<div style="color:var(--text3);font-size:12px">none</div>'
        }
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
  infoEl.querySelectorAll<HTMLElement>('[data-version]').forEach((tag) => {
    tag.addEventListener('click', async () => {
      const ver = tag.dataset.version!;
      if (ver === m.version) return;
      try {
        const modules = await apiFetch<Module[]>(
          `/modules/?limit=1&version=${encodeURIComponent(ver)}&repo=${encodeURIComponent(m.repo)}&module_path=${encodeURIComponent(m.module_path)}`,
        );
        const match = modules[0];
        if (match) {
          selectedModule = match;
          await renderInfoTab(match);
        }
      } catch {
        // Ignore
      }
    });
  });
}
