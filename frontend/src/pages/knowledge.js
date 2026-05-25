import { apiFetch, esc } from '../api';
const CONVENTION_DIMS = [
    { key: 'naming', label: 'Naming' },
    { key: 'vars', label: 'Variables' },
    { key: 'codeploy', label: 'Code & Deploy' },
    { key: 'tagging', label: 'Tagging' },
    { key: 'layout', label: 'Layout' },
    { key: 'versions', label: 'Versions' },
];
let selectedRef = null;
export function initKnowledgePage() {
    const kindFilter = document.getElementById('knowledgeKindFilter');
    const repoFilter = document.getElementById('knowledgeRepoFilter');
    const search = document.getElementById('knowledgeSearch');
    kindFilter.addEventListener('change', () => loadModuleRefs());
    repoFilter.addEventListener('change', () => loadModuleRefs());
    let debounce;
    search.addEventListener('input', () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => loadModuleRefs(), 250);
    });
    loadConsumerRepoOptions();
    loadModuleRefs();
}
async function loadConsumerRepoOptions() {
    try {
        const repos = await apiFetch('/snippets/consumer-repos');
        const sel = document.getElementById('knowledgeRepoFilter');
        // Remove any previously injected repo options (everything after the
        // static "all consumer repos" placeholder). initKnowledgePage() runs
        // every time the user navigates to this tab, so without this cleanup
        // the list grows by 8 entries per visit.
        while (sel.options.length > 1) {
            sel.remove(1);
        }
        for (const r of repos) {
            const opt = document.createElement('option');
            opt.value = r;
            opt.textContent = r;
            sel.appendChild(opt);
        }
    }
    catch { /* ignore */ }
}
async function loadModuleRefs() {
    const kindFilter = document.getElementById('knowledgeKindFilter').value;
    const repoFilter = document.getElementById('knowledgeRepoFilter').value;
    const search = document.getElementById('knowledgeSearch').value.trim();
    const params = new URLSearchParams();
    if (kindFilter)
        params.set('kind', kindFilter);
    if (repoFilter)
        params.set('consumer_repo', repoFilter);
    if (search)
        params.set('q', search);
    const qs = params.toString();
    const url = '/snippets/module-refs' + (qs ? '?' + qs : '');
    try {
        const refs = await apiFetch(url);
        renderModuleRefList(refs);
        document.getElementById('knowledgeCount').textContent = String(refs.length);
    }
    catch {
        document.getElementById('knowledgeList').innerHTML =
            '<div class="empty">Failed to load module refs</div>';
    }
}
function parseModuleRef(ref) {
    const idx = ref.indexOf('/');
    if (idx === -1)
        return { name: ref, repo: '' };
    return { repo: ref.slice(0, idx), name: ref.slice(idx + 1) };
}
function renderModuleRefList(refs) {
    const container = document.getElementById('knowledgeList');
    if (!refs.length) {
        container.innerHTML = '<div class="empty">No knowledge snippets found</div>';
        return;
    }
    container.innerHTML = refs.map((r) => {
        const { name, repo } = parseModuleRef(r.module_ref);
        return `
    <div class="module-item snippet-card${r.module_ref === selectedRef ? ' selected' : ''}"
         data-ref="${esc(r.module_ref)}">
      <div class="module-item-name">${esc(name)}</div>
      <div class="module-item-repo">${esc(repo)}</div>
      <div class="module-item-tags">
        ${r.usage_count ? `<span class="tag cyan">${r.usage_count} usage${r.usage_count !== 1 ? 's' : ''}</span>` : ''}
        ${r.convention_count ? `<span class="tag green">${r.convention_count} convention${r.convention_count !== 1 ? 's' : ''}</span>` : ''}
      </div>
    </div>`;
    }).join('');
    container.querySelectorAll('.snippet-card').forEach((el) => {
        el.addEventListener('click', () => {
            const ref = el.dataset.ref;
            container.querySelectorAll('.module-item').forEach((m) => m.classList.remove('selected'));
            el.classList.add('selected');
            selectedRef = ref;
            loadModuleRefDetail(ref);
        });
    });
}
async function loadModuleRefDetail(moduleRef) {
    const detail = document.getElementById('knowledgeDetail');
    detail.innerHTML = '<div class="placeholder-msg"><div>Loading...</div></div>';
    try {
        const data = await apiFetch(`/snippets/module-refs/${encodeURIComponent(moduleRef)}`);
        renderDetail(data);
    }
    catch {
        detail.innerHTML = '<div class="placeholder-msg"><div>Failed to load detail</div></div>';
    }
}
function renderDetail(data) {
    const detail = document.getElementById('knowledgeDetail');
    const conventionsHtml = CONVENTION_DIMS.map((dim) => {
        const snippet = data.conventions[dim.key];
        if (!snippet) {
            return `
        <div class="detail-card snippet-card-dim empty-dim">
          <div class="detail-card-title">${esc(dim.label)}</div>
          <div class="snippet-summary" style="color:var(--text3)">No data</div>
        </div>`;
        }
        return `
      <div class="detail-card snippet-card-dim">
        <div class="detail-card-title">
          ${esc(dim.label)}
          <span class="evidence-badge">${snippet.evidence_count} evidence</span>
        </div>
        <div class="snippet-summary">${esc(snippet.summary)}</div>
      </div>`;
    }).join('');
    const usagesHtml = data.usages.length
        ? data.usages.map((u) => renderUsageItem(u)).join('')
        : '<div class="empty">No usage snippets</div>';
    detail.innerHTML = `
    <div class="detail-title">
      ${esc(data.module_ref)}
    </div>

    <div style="margin-top:8px">
      <div class="detail-card-title" style="padding:0 0 8px 0">Conventions</div>
      <div class="detail-grid">${conventionsHtml}</div>
    </div>

    <div style="margin-top:16px">
      <div class="detail-card-title" style="padding:0 0 8px 0">
        Usages <span class="evidence-badge">${data.usages.length}</span>
      </div>
      <div class="snippet-usages-list">${usagesHtml}</div>
    </div>
  `;
}
function renderUsageItem(u) {
    return `
    <div class="detail-card" style="margin-bottom:8px">
      <div class="snippet-summary">${esc(u.summary)}</div>
      <div style="margin-top:6px;display:flex;gap:10px;font-size:11px;color:var(--text3)">
        ${u.source_locator ? `<span>${esc(u.source_locator)}</span>` : ''}
        ${u.consumer_repo ? `<span class="tag">${esc(u.consumer_repo)}</span>` : ''}
      </div>
    </div>`;
}
