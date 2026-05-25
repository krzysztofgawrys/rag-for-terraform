import { apiFetch, esc, toast } from '../api';
export function initMigrationPage() {
    document.getElementById('migBtn').addEventListener('click', runMigration);
}
async function runMigration() {
    const oldTag = document.getElementById('oldTag').value.trim();
    const newTag = document.getElementById('newTag').value.trim();
    if (!oldTag || !newTag) {
        toast('Fill in both tags', 'error');
        return;
    }
    const btn = document.getElementById('migBtn');
    btn.innerHTML = '<span class="spinner"></span>';
    btn.disabled = true;
    try {
        const r = await apiFetch('/query/tag-migration', {
            method: 'POST',
            body: JSON.stringify({ old_tag: oldTag, new_tag: newTag, dry_run: true }),
        });
        document.getElementById('migrationResult').style.display = 'grid';
        document.getElementById('affectedCount').textContent = String(r.affected_modules.length);
        document.getElementById('dependentsCount').textContent = String(r.dependent_modules.length);
        document.getElementById('affectedList').innerHTML =
            r.affected_modules.map((m) => `<div class="list-item">${esc(m)}</div>`).join('') ||
                '<div style="color:var(--text3);font-size:12px;padding:8px 0">None found</div>';
        document.getElementById('dependentsList').innerHTML =
            r.dependent_modules.map((m) => `<div class="list-item">${esc(m)}</div>`).join('') ||
                '<div style="color:var(--text3);font-size:12px;padding:8px 0">None</div>';
        document.getElementById('migrationPlan').textContent = r.migration_plan;
    }
    catch (e) {
        toast('Migration analysis failed: ' + e.message, 'error');
    }
    finally {
        btn.innerHTML = '<span>&#x25B6; Analyze Impact</span>';
        btn.disabled = false;
    }
}
