/**
 * Reusable multi-select dropdown with chip display and search.
 *
 * Usage:
 *   const cs = new ChipSelect(containerEl, { placeholder: 'select...' });
 *   cs.setOptions(['a','b','c']);
 *   cs.getSelected();          // → ['a','c']
 *   cs.onChange = (vals) => …;
 */
export class ChipSelect {
    constructor(container, opts = {}) {
        this.options = [];
        this.selected = new Set();
        this.open = false;
        this.onChange = null;
        this.container = container;
        this.container.classList.add('chip-select');
        // Field area (chips + placeholder click target)
        this.field = document.createElement('div');
        this.field.className = 'chip-select-field';
        this.field.innerHTML = `<span class="chip-select-placeholder">${opts.placeholder || 'select...'}</span>`;
        // Dropdown
        this.dropdown = document.createElement('div');
        this.dropdown.className = 'chip-select-dropdown';
        this.dropdown.style.display = 'none';
        this.searchInput = document.createElement('input');
        this.searchInput.type = 'text';
        this.searchInput.className = 'chip-select-search';
        this.searchInput.placeholder = 'search...';
        this.listEl = document.createElement('div');
        this.listEl.className = 'chip-select-list';
        this.dropdown.appendChild(this.searchInput);
        this.dropdown.appendChild(this.listEl);
        this.container.appendChild(this.field);
        this.container.appendChild(this.dropdown);
        // Events
        this.field.addEventListener('click', (e) => {
            // If clicked on a chip-remove button, handle removal instead
            const rm = e.target.closest('.chip-remove');
            if (rm) {
                const val = rm.dataset.val;
                this.deselect(val);
                return;
            }
            this.toggle();
        });
        this.searchInput.addEventListener('input', () => this.renderList());
        // Close on outside click
        document.addEventListener('mousedown', (e) => {
            if (this.open && !this.container.contains(e.target)) {
                this.close();
            }
        });
    }
    setOptions(options) {
        this.options = options;
        // Remove selected values that are no longer in options
        for (const v of this.selected) {
            if (!options.includes(v))
                this.selected.delete(v);
        }
        this.renderField();
        if (this.open)
            this.renderList();
    }
    getSelected() {
        return [...this.selected];
    }
    toggle() {
        this.open ? this.close() : this.openDropdown();
    }
    openDropdown() {
        this.open = true;
        this.dropdown.style.display = '';
        this.searchInput.value = '';
        this.renderList();
        requestAnimationFrame(() => this.searchInput.focus());
    }
    close() {
        this.open = false;
        this.dropdown.style.display = 'none';
    }
    select(val) {
        this.selected.add(val);
        this.renderField();
        this.renderList();
        this.notify();
    }
    deselect(val) {
        this.selected.delete(val);
        this.renderField();
        if (this.open)
            this.renderList();
        this.notify();
    }
    notify() {
        this.onChange?.(this.getSelected());
    }
    renderField() {
        const chips = [...this.selected];
        if (!chips.length) {
            this.field.innerHTML = `<span class="chip-select-placeholder">${esc(this.field.querySelector('.chip-select-placeholder')?.textContent || 'select...')}</span>`;
            return;
        }
        this.field.innerHTML = chips
            .map((v) => `<span class="chip-select-chip">${esc(v)}<button class="chip-remove" data-val="${esc(v)}">&times;</button></span>`)
            .join('');
    }
    renderList() {
        const q = this.searchInput.value.toLowerCase();
        const filtered = this.options.filter((o) => o.toLowerCase().includes(q));
        if (!filtered.length) {
            this.listEl.innerHTML = '<div class="chip-select-empty">no options</div>';
            return;
        }
        this.listEl.innerHTML = filtered
            .map((o) => {
            const sel = this.selected.has(o);
            return `<div class="chip-select-option ${sel ? 'selected' : ''}" data-val="${esc(o)}">
          <span class="chip-select-check">${sel ? '&#x2713;' : ''}</span>
          ${esc(o)}
        </div>`;
        })
            .join('');
        this.listEl.querySelectorAll('.chip-select-option').forEach((el) => {
            el.addEventListener('click', () => {
                const val = el.dataset.val;
                if (this.selected.has(val)) {
                    this.deselect(val);
                }
                else {
                    this.select(val);
                }
            });
        });
    }
}
function esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
