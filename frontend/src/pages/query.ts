import { Marked } from 'marked';
import { markedHighlight } from 'marked-highlight';
import hljs from 'highlight.js/lib/core';
import hljsBash from 'highlight.js/lib/languages/bash';
import hljsJson from 'highlight.js/lib/languages/json';
import hclLanguage from '../hcl-lang';
import { apiFetch, esc, toast } from '../api';
import type { QueryType, Source } from '../types';
import { ChipSelect } from '../chip-select';

hljs.registerLanguage('hcl', hclLanguage);
hljs.registerLanguage('terraform', hclLanguage);
hljs.registerLanguage('tf', hclLanguage);
hljs.registerLanguage('bash', hljsBash);
hljs.registerLanguage('json', hljsJson);

const marked = new Marked(
  markedHighlight({
    highlight(code: string, lang: string) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value;
      }
      return hljs.highlightAuto(code).value;
    },
  }),
);

const API =
  window.location.port === '3000'
    ? `${window.location.protocol}//${window.location.hostname}:8000`
    : '';

let queryType: QueryType = 'compose';
let repoChipSelect: ChipSelect;
let tagChipSelect: ChipSelect;
let versionChipSelect: ChipSelect;
let activeController: AbortController | null = null;
let userAborted = false;
let renderDirty = false;
let renderRaf = 0;
let userScrolledUp = false;

const placeholders: Record<QueryType, string> = {
  compose: 'e.g. Build an ECS Fargate service with ALB, or create an S3 bucket with versioning...',
  optimize: 'e.g. Review our RDS module for security issues and missing tags...',
  audit: 'e.g. Audit all modules in the prod repo for missing environment tags...',
  search: 'e.g. Which modules create VPCs with private subnets?',
};

export function initQueryPage(): void {
  document.getElementById('typeGrid')!.addEventListener('click', (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('.type-btn');
    if (!btn?.dataset.type) return;

    queryType = btn.dataset.type as QueryType;
    document.querySelectorAll('.type-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    (document.getElementById('queryText') as HTMLTextAreaElement).placeholder =
      placeholders[queryType];
  });

  // Chip-select filters
  repoChipSelect = new ChipSelect(document.getElementById('queryRepoFilter')!, {
    placeholder: 'select repos...',
  });
  tagChipSelect = new ChipSelect(document.getElementById('queryTagFilter')!, {
    placeholder: 'select tags...',
  });
  versionChipSelect = new ChipSelect(document.getElementById('queryVersionFilter')!, {
    placeholder: 'select versions...',
  });

  loadFilterOptions();

  document.getElementById('runBtn')!.addEventListener('click', runQuery);
  document.getElementById('stopBtn')!.addEventListener('click', () => {
    if (activeController) {
      userAborted = true;
      activeController.abort();
    }
  });
}

async function loadFilterOptions(): Promise<void> {
  try {
    const [repos, tags, versions] = await Promise.all([
      apiFetch<string[]>('/modules/repos/all'),
      apiFetch<{ tag: string; count: number }[]>('/modules/tags/all'),
      apiFetch<string[]>('/modules/versions/all'),
    ]);
    repoChipSelect.setOptions(repos);
    tagChipSelect.setOptions(tags.map((t) => t.tag));
    versionChipSelect.setOptions(versions);
  } catch {
    // filters will remain empty — non-critical
  }
}

async function runQuery(): Promise<void> {
  const q = (document.getElementById('queryText') as HTMLTextAreaElement).value.trim();
  if (!q) {
    toast('Enter a query first', 'error');
    return;
  }

  const btn = document.getElementById('runBtn')! as HTMLButtonElement;
  const stopBtn = document.getElementById('stopBtn')! as HTMLButtonElement;
  btn.innerHTML = '<span class="spinner"></span>';
  btn.disabled = true;
  stopBtn.style.display = '';
  userAborted = false;

  userScrolledUp = false;

  const area = document.getElementById('outputArea')!;
  area.innerHTML = `
    <div class="output-meta">
      <span>Searching...</span>
    </div>
    <div class="answer-block" id="streamAnswer"></div>
  `;

  // Track whether user has scrolled up (to disable auto-scroll)
  const answerBlock = document.getElementById('streamAnswer')!;
  answerBlock.addEventListener('scroll', () => {
    const el = answerBlock;
    // "At bottom" if within 40px of the end
    userScrolledUp = el.scrollTop + el.clientHeight < el.scrollHeight - 40;
  });

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
  const STALL_TIMEOUT_MS = 360_000;
  const controller = new AbortController();
  activeController = controller;
  let stallTimer: ReturnType<typeof setTimeout> | null = null;
  const armStallTimer = (): void => {
    if (stallTimer) clearTimeout(stallTimer);
    stallTimer = setTimeout(() => controller.abort(), STALL_TIMEOUT_MS);
  };
  const disarmStallTimer = (): void => {
    if (stallTimer) {
      clearTimeout(stallTimer);
      stallTimer = null;
    }
  };

  let streamError: string | null = null;
  let answerText = '';
  let sources: Source[] = [];

  try {
    armStallTimer();

    const response = await fetch(API + '/query/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: controller.signal,
    });

    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);

    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      armStallTimer(); // reset watchdog on every chunk

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let data: any;
        try {
          data = JSON.parse(line.slice(6));
        } catch {
          continue; // malformed line — ignore
        }

        if (data.type === 'sources') {
          sources = data.sources;
          const meta = document.querySelector('.output-meta')!;
          meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency">streaming...</span>`;
        } else if (data.type === 'agent_status') {
          const meta = document.querySelector('.output-meta')!;
          const toolInfo = data.tool_calls ? ` (${data.tool_calls} tool calls)` : '';
          meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency">${esc(data.message)}${toolInfo}</span>`;
        } else if (data.type === 'reasoning_start') {
          // Create a collapsible reasoning panel (open while streaming)
          const answerEl = document.getElementById('streamAnswer')!;
          const details = document.createElement('details');
          details.className = 'reasoning-panel';
          details.setAttribute('open', '');
          details.dataset.turn = String(data.turn);
          details.innerHTML = `<summary>Thinking (turn ${data.turn})...</summary><pre class="reasoning-content"></pre>`;
          answerEl.appendChild(details);
        } else if (data.type === 'reasoning') {
          // Append reasoning tokens to the current open panel + auto-scroll
          const panels = document.querySelectorAll('.reasoning-panel[open]');
          if (panels.length) {
            const pre = panels[panels.length - 1].querySelector('.reasoning-content');
            if (pre) {
              pre.textContent += data.token;
              pre.scrollTop = pre.scrollHeight;
            }
          }
          // Auto-scroll the answer block too
          if (!userScrolledUp) {
            const block = document.getElementById('streamAnswer');
            if (block) block.scrollTop = block.scrollHeight;
          }
        } else if (data.type === 'reasoning_end') {
          // Collapse the panel now that reasoning is done
          const panels = document.querySelectorAll<HTMLDetailsElement>(`.reasoning-panel[data-turn="${data.turn}"]`);
          panels.forEach(p => p.removeAttribute('open'));
        } else if (data.type === 'tool_call') {
          const answerEl = document.getElementById('streamAnswer')!;
          const details = document.createElement('details');
          details.className = 'tool-call-indicator';
          const inputBrief = data.input ? String(data.input).slice(0, 120) : '';
          const inputDetail = data.input_full || data.input || '';
          details.innerHTML =
            `<summary><span class="tool-icon">&#9881;</span> <span class="tool-name">${esc(data.tool)}</span> <span class="tool-args">${esc(inputBrief)}</span><span class="tool-spinner"></span></summary>` +
            `<div class="tool-detail"><div class="tool-detail-label">Input</div><pre class="tool-detail-pre">${esc(inputDetail)}</pre></div>`;
          answerEl.appendChild(details);
          // Auto-scroll when tool calls appear
          if (!userScrolledUp) {
            const block = document.getElementById('streamAnswer');
            if (block) block.scrollTop = block.scrollHeight;
          }
        } else if (data.type === 'tool_result') {
          // Add result to the last tool-call and mark done
          const indicators = document.querySelectorAll('.tool-call-indicator');
          if (indicators.length) {
            const last = indicators[indicators.length - 1] as HTMLElement;
            last.classList.add('tool-done');
            const detail = last.querySelector('.tool-detail');
            if (detail) {
              const resultText = data.detail || data.summary || '';
              detail.insertAdjacentHTML('beforeend',
                `<div class="tool-detail-label">Result</div><pre class="tool-detail-pre">${esc(resultText)}</pre>`);
            }
            // Auto-scroll when tool results arrive
            if (!userScrolledUp) {
              const block = document.getElementById('streamAnswer');
              if (block) block.scrollTop = block.scrollHeight;
            }
          }
        } else if (data.type === 'token') {
          answerText += data.token;
          const answerEl = document.getElementById('streamAnswer')!;
          // Preserve reasoning panels and tool indicators — render tokens
          // into a dedicated output div so innerHTML doesn't wipe them.
          let outputDiv = answerEl.querySelector('.agent-output') as HTMLElement;
          if (!outputDiv) {
            // First token: collapse reasoning panels, keep tool indicators visible
            answerEl.querySelectorAll<HTMLDetailsElement>('.reasoning-panel').forEach(p => p.removeAttribute('open'));
            outputDiv = document.createElement('div');
            outputDiv.className = 'agent-output';
            answerEl.appendChild(outputDiv);
          }
          // Throttle markdown re-render to once per animation frame (~16ms)
          // to avoid full reparse + reflow on every single token.
          if (!renderDirty) {
            renderDirty = true;
            renderRaf = requestAnimationFrame(() => {
              renderDirty = false;
              const od = document.querySelector('.agent-output') as HTMLElement;
              const block = document.getElementById('streamAnswer');
              if (od) {
                // Save scroll position before re-render
                const prevScroll = block ? block.scrollTop : 0;
                od.innerHTML = marked.parse(answerText) as string;
                if (block) {
                  if (!userScrolledUp) {
                    // Auto-scroll to bottom
                    block.scrollTop = block.scrollHeight;
                  } else {
                    // Restore scroll position so re-render doesn't jump
                    block.scrollTop = prevScroll;
                  }
                }
              }
            });
          }
        } else if (data.type === 'error') {
          streamError = data.message || 'LLM stream failed';
          const meta = document.querySelector('.output-meta')!;
          meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency" style="color:var(--red,#e06c75)">error after ${data.latency_ms}ms</span>`;
        } else if (data.type === 'done') {
          const meta = document.querySelector('.output-meta')!;
          const extra = data.turns ? ` &middot; ${data.turns} turns, ${data.tool_calls || 0} tools` : '';
          if (streamError) {
            meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency" style="color:var(--red,#e06c75)">failed after ${data.latency_ms}ms</span>`;
          } else {
            meta.innerHTML = `<span>${sources.length} sources</span><span>&middot;</span><span class="latency">${data.latency_ms}ms${extra}</span>`;
          }
        }
      }
    }

    // Cancel any pending throttled render before final render
    if (renderRaf) { cancelAnimationFrame(renderRaf); renderRaf = 0; }
    renderDirty = false;

    if (streamError) {
      toast('LLM error: ' + streamError, 'error');
    }
    // Final render — show whatever we got, with sources
    renderFinalResult(answerText, sources);
  } catch (e) {
    if (renderRaf) { cancelAnimationFrame(renderRaf); renderRaf = 0; }
    renderDirty = false;
    const err = e as Error;
    if (err.name === 'AbortError') {
      if (userAborted) {
        toast('Query stopped', '');
        const meta = document.querySelector('.output-meta');
        if (meta) {
          meta.innerHTML = `<span style="color:var(--yellow,#e5c07b)">Stopped by user</span>`;
        }
        // keep partial answer visible
        if (answerText) renderFinalResult(answerText, sources);
      } else {
        toast(`Query timed out after ${STALL_TIMEOUT_MS / 1000}s of silence`, 'error');
        const meta = document.querySelector('.output-meta');
        if (meta) {
          meta.innerHTML = `<span style="color:var(--red,#e06c75)">Request timed out</span>`;
        }
      }
    } else {
      toast('Query failed: ' + err.message, 'error');
      const meta = document.querySelector('.output-meta');
      if (meta) {
        meta.innerHTML = `<span style="color:var(--red,#e06c75)">Request failed</span>`;
      }
    }
  } finally {
    disarmStallTimer();
    activeController = null;
    btn.innerHTML = '<span>&#x25B6; Run</span>';
    btn.disabled = false;
    stopBtn.style.display = 'none';
  }
}

function renderFinalResult(answer: string, sources: Source[]): void {
  const answerBlock = document.getElementById('streamAnswer')!;

  // Final markdown render — preserve reasoning panels + tool indicators from agent mode
  const agentElements = answerBlock.querySelectorAll('.reasoning-panel, .tool-call-indicator');
  const preserved = Array.from(agentElements).map(el => el.cloneNode(true));
  answerBlock.innerHTML = '';
  preserved.forEach(el => answerBlock.appendChild(el));
  const outputDiv = document.createElement('div');
  outputDiv.className = 'agent-output';
  outputDiv.innerHTML = marked.parse(answer) as string;
  answerBlock.appendChild(outputDiv);

  // Append sources
  if (sources.length) {
    const sourcesHtml = `
      <div>
        <div class="form-label" style="margin-bottom:8px">Sources</div>
        <div class="sources-list">
          ${sources
            .map(
              (s) => `
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
          `,
            )
            .join('')}
        </div>
      </div>
    `;
    answerBlock.insertAdjacentHTML('afterend', sourcesHtml);
  }
}
