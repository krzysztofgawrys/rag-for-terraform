import { Marked } from 'marked';
import { markedHighlight } from 'marked-highlight';
import hljs from 'highlight.js/lib/core';
import hljsBash from 'highlight.js/lib/languages/bash';
import hljsJson from 'highlight.js/lib/languages/json';
import hclLanguage from '../hcl-lang';
import { apiFetch, esc, toast } from '../api';
import { ChipSelect } from '../chip-select';
hljs.registerLanguage('hcl', hclLanguage);
hljs.registerLanguage('terraform', hclLanguage);
hljs.registerLanguage('tf', hclLanguage);
hljs.registerLanguage('bash', hljsBash);
hljs.registerLanguage('json', hljsJson);
const marked = new Marked(markedHighlight({
    highlight(code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    },
}));
const API = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? 'http://localhost:8000'
    : '';
let queryType = 'compose';
let repoChipSelect;
let tagChipSelect;
let versionChipSelect;
let activeController = null;
let userAborted = false;
const placeholders = {
    generate: 'e.g. Create an S3 bucket with versioning and KMS encryption...',
    compose: 'e.g. Build me a full ECS Fargate web app stack with VPC, ALB, ACM, ECR, SSM...',
    optimize: 'e.g. Review our RDS module for security issues and missing tags...',
    audit: 'e.g. Audit all modules in the prod repo for missing environment tags...',
    search: 'e.g. Which modules create VPCs with private subnets?',
};
export function initQueryPage() {
    document.getElementById('typeGrid').addEventListener('click', (e) => {
        const btn = e.target.closest('.type-btn');
        if (!btn?.dataset.type)
            return;
        queryType = btn.dataset.type;
        document.querySelectorAll('.type-btn').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('queryText').placeholder =
            placeholders[queryType];
    });
    // Chip-select filters
    repoChipSelect = new ChipSelect(document.getElementById('queryRepoFilter'), {
        placeholder: 'select repos...',
    });
    tagChipSelect = new ChipSelect(document.getElementById('queryTagFilter'), {
        placeholder: 'select tags...',
    });
    versionChipSelect = new ChipSelect(document.getElementById('queryVersionFilter'), {
        placeholder: 'select versions...',
    });
    loadFilterOptions();
    document.getElementById('runBtn').addEventListener('click', runQuery);
    document.getElementById('stopBtn').addEventListener('click', () => {
        if (activeController) {
            userAborted = true;
            activeController.abort();
        }
    });
}
async function loadFilterOptions() {
    try {
        const [repos, tags, versions] = await Promise.all([
            apiFetch('/modules/repos/all'),
            apiFetch('/modules/tags/all'),
            apiFetch('/modules/versions/all'),
        ]);
        repoChipSelect.setOptions(repos);
        tagChipSelect.setOptions(tags.map((t) => t.tag));
        versionChipSelect.setOptions(versions);
    }
    catch {
        // filters will remain empty — non-critical
    }
}
async function runQuery() {
    const q = document.getElementById('queryText').value.trim();
    if (!q) {
        toast('Enter a query first', 'error');
        return;
    }
    const btn = document.getElementById('runBtn');
    const stopBtn = document.getElementById('stopBtn');
    btn.innerHTML = '<span class="spinner"></span>';
    btn.disabled = true;
    stopBtn.style.display = '';
    userAborted = false;
    const area = document.getElementById('outputArea');
    area.innerHTML = `
    <div class="output-meta">
      <span>Searching...</span>
    </div>
    <div class="answer-block" id="streamAnswer"></div>
  `;
    const repos = repoChipSelect.getSelected();
    const tags = tagChipSelect.getSelected();
    const versions = versionChipSelect.getSelected();
    const body = JSON.stringify({
        query: q,
        query_type: queryType,
        repo_filter: repos.length ? repos : null,
        tag_filter: tags.length ? tags : null,
        version_filter: versions.length ? versions : null,
        top_k: 5,
    });
    // Watchdog: if the stream stalls (no events for STALL_TIMEOUT_MS),
    // abort the request so the user is not stuck on a spinning button.
    const STALL_TIMEOUT_MS = 120000;
    const controller = new AbortController();
    activeController = controller;
    let stallTimer = null;
    const armStallTimer = () => {
        if (stallTimer)
            clearTimeout(stallTimer);
        stallTimer = setTimeout(() => controller.abort(), STALL_TIMEOUT_MS);
    };
    const disarmStallTimer = () => {
        if (stallTimer) {
            clearTimeout(stallTimer);
            stallTimer = null;
        }
    };
    let streamError = null;
    let answerText = '';
    let sources = [];
    try {
        armStallTimer();
        const response = await fetch(API + '/query/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body,
            signal: controller.signal,
        });
        if (!response.ok)
            throw new Error(`${response.status} ${response.statusText}`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done)
                break;
            armStallTimer(); // reset watchdog on every chunk
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (!line.startsWith('data: '))
                    continue;
                let data;
                try {
                    data = JSON.parse(line.slice(6));
                }
                catch {
                    continue; // malformed line — ignore
                }
                if (data.type === 'sources') {
                    sources = data.sources;
                    const meta = document.querySelector('.output-meta');
                    meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency">streaming...</span>`;
                }
                else if (data.type === 'token') {
                    answerText += data.token;
                    const answerEl = document.getElementById('streamAnswer');
                    answerEl.innerHTML = marked.parse(answerText);
                }
                else if (data.type === 'error') {
                    streamError = data.message || 'LLM stream failed';
                    const meta = document.querySelector('.output-meta');
                    meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency" style="color:var(--red,#e06c75)">error after ${data.latency_ms}ms</span>`;
                }
                else if (data.type === 'done') {
                    const meta = document.querySelector('.output-meta');
                    if (streamError) {
                        meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency" style="color:var(--red,#e06c75)">failed after ${data.latency_ms}ms</span>`;
                    }
                    else {
                        meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency">${data.latency_ms}ms</span>`;
                    }
                }
            }
        }
        if (streamError) {
            toast('LLM error: ' + streamError, 'error');
        }
        // Final render — show whatever we got, with sources
        renderFinalResult(answerText, sources);
    }
    catch (e) {
        const err = e;
        if (err.name === 'AbortError') {
            if (userAborted) {
                toast('Query stopped');
                const meta = document.querySelector('.output-meta');
                if (meta) {
                    meta.innerHTML = `<span style="color:var(--yellow,#e5c07b)">Stopped by user</span>`;
                }
                if (answerText) renderFinalResult(answerText, sources);
            } else {
                toast(`Query timed out after ${STALL_TIMEOUT_MS / 1000}s of silence`, 'error');
                const meta = document.querySelector('.output-meta');
                if (meta) {
                    meta.innerHTML = `<span style="color:var(--red,#e06c75)">Request timed out</span>`;
                }
            }
        }
        else {
            toast('Query failed: ' + err.message, 'error');
            const meta = document.querySelector('.output-meta');
            if (meta) {
                meta.innerHTML = `<span style="color:var(--red,#e06c75)">Request failed</span>`;
            }
        }
    }
    finally {
        disarmStallTimer();
        activeController = null;
        btn.innerHTML = '<span>&#x25B6; Run</span>';
        btn.disabled = false;
        stopBtn.style.display = 'none';
    }
}
function renderFinalResult(answer, sources) {
    const answerBlock = document.getElementById('streamAnswer');
    // Final markdown render
    answerBlock.innerHTML = marked.parse(answer);
    // Append sources
    if (sources.length) {
        const sourcesHtml = `
      <div>
        <div class="form-label" style="margin-bottom:8px">Sources</div>
        <div class="sources-list">
          ${sources
            .map((s) => `
            <div class="source-item">
              <span class="source-score">${(s.similarity * 100).toFixed(0)}%</span>
              <div class="source-info">
                <div class="source-name">${esc(s.module_name)}</div>
                <div class="source-repo">${esc(s.repo)} &middot; ${esc(s.module_path)} &middot; <span class="tag" style="color:var(--purple);border-color:var(--purple)">${esc(s.version || '')}</span></div>
              </div>
              <div style="display:flex;gap:4px;flex-wrap:wrap">
                ${(s.tags || [])
            .slice(0, 3)
            .map((t) => `<span class="tag">${esc(t)}</span>`)
            .join('')}
              </div>
            </div>
          `)
            .join('')}
        </div>
      </div>
    `;
        answerBlock.insertAdjacentHTML('afterend', sourcesHtml);
    }
}
