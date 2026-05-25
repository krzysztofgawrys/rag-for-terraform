export type PageId = 'modules' | 'query' | 'jobs' | 'usage' | 'knowledge' | 'auditlogs' | 'login';

const validPages: Set<string> = new Set(['modules', 'query', 'jobs', 'usage', 'knowledge', 'auditlogs', 'login']);

type NavigateCallback = (pageId: PageId) => void;

let onNavigate: NavigateCallback | undefined;

export function initRouter(callback: NavigateCallback): void {
  onNavigate = callback;
  window.addEventListener('hashchange', navigate);

  // Nav button clicks — always trigger navigate, even if already on that page
  document.querySelectorAll<HTMLButtonElement>('nav button[data-page]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = '#/' + btn.dataset.page;
      if (location.hash === target) {
        navigate();
      } else {
        location.hash = target;
      }
    });
  });

  navigate();
}

export function navigateTo(pageId: PageId): void {
  location.hash = '#/' + pageId;
}

function navigate(): void {
  const hash = location.hash.replace('#/', '');
  const pageId: PageId = validPages.has(hash) ? (hash as PageId) : 'modules';

  // Toggle page visibility
  document.querySelectorAll('.page').forEach((p) => p.classList.remove('active'));
  document.getElementById(`page-${pageId}`)?.classList.add('active');

  // Toggle nav active state
  document.querySelectorAll<HTMLButtonElement>('nav button[data-page]').forEach((b) => {
    b.classList.toggle('active', b.dataset.page === pageId);
  });

  // Hide nav and stats when on login page
  const navEl = document.querySelector('nav');
  const statsBar = document.getElementById('statsBar');
  if (pageId === 'login') {
    navEl?.classList.add('hidden');
    statsBar?.classList.add('hidden');
  } else {
    navEl?.classList.remove('hidden');
    statsBar?.classList.remove('hidden');
  }

  onNavigate?.(pageId);
}
