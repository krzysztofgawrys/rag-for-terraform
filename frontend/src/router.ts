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

  // Hide app shell when on login/landing page
  const appHeader = document.getElementById('appHeader');
  const appMain = document.getElementById('appMain');
  const siteFooter = document.getElementById('siteFooter');
  if (pageId === 'login') {
    if (appHeader) appHeader.style.display = 'none';
    if (appMain) appMain.style.display = 'none';
    if (siteFooter) siteFooter.style.display = 'none';
  } else {
    if (appHeader) appHeader.style.display = '';
    if (appMain) appMain.style.display = '';
    if (siteFooter) siteFooter.style.display = 'block';
  }

  onNavigate?.(pageId);
}
