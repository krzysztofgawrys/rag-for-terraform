import puppeteer from 'puppeteer';
import { mkdir } from 'fs/promises';

const BASE = 'https://terraform-rag.io';
const OUT = './docs/screenshots';
const WIDTH = 1440;
const HEIGHT = 900;

const LOGIN_EMAIL = 'gawrys@protonmail.ch';
const LOGIN_PASSWORD = '1fa5f9d52304ec41e11ec3d355dfa236';

await mkdir(OUT, { recursive: true });

const browser = await puppeteer.launch({
  headless: true,
  args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'],
});

const page = await browser.newPage();
await page.setViewport({ width: WIDTH, height: HEIGHT });

// -- Login ------------------------------------------------------------------
console.log('Logging in...');
await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 20000 });
await new Promise(r => setTimeout(r, 2000));

const emailInput = await page.$('input[type="email"], input[name="email"]');
const passInput = await page.$('input[type="password"]');
if (emailInput && passInput) {
  await emailInput.type(LOGIN_EMAIL);
  await passInput.type(LOGIN_PASSWORD);
  await new Promise(r => setTimeout(r, 300));
  const loginBtn = await page.$('button[type="submit"], form button');
  if (loginBtn) await loginBtn.click();
  await new Promise(r => setTimeout(r, 3000));
  console.log('  Logged in.');
} else {
  console.log('  No login form (auth may be disabled).');
}

// Helper
async function snap(name, fn) {
  console.log(`  -> ${name}`);
  if (fn) await fn();
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
}

// 1. Modules - click a VPC module for a good detail panel
await snap('01-modules', async () => {
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 20000 });
  await new Promise(r => setTimeout(r, 2000));
  // Click Modules tab (should be default, but ensure)
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('nav button')].find(b => b.textContent.includes('Modules'));
    if (btn) btn.click();
  });
  await new Promise(r => setTimeout(r, 1500));
  // Type "vpc" in search to find VPC module
  await page.evaluate(() => {
    const input = document.getElementById('moduleSearch');
    if (input) { input.value = 'vpc'; input.dispatchEvent(new Event('input', { bubbles: true })); }
  });
  await new Promise(r => setTimeout(r, 1500));
  // Click first module item
  await page.evaluate(() => {
    const item = document.querySelector('#moduleList .module-item');
    if (item) item.click();
  });
  await new Promise(r => setTimeout(r, 1500));
});

// 2. Query Compose - start streaming
await snap('02-query-compose', async () => {
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('nav button')].find(b => b.textContent.includes('Query'));
    if (btn) btn.click();
  });
  await new Promise(r => setTimeout(r, 1500));
  // Type query using evaluate to avoid click issues
  await page.evaluate(() => {
    const ta = document.getElementById('queryText');
    if (ta) {
      ta.value = 'Create an ECS Fargate service with ALB, auto-scaling, and CloudWatch logging';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  await new Promise(r => setTimeout(r, 500));
  // Click Run
  await page.evaluate(() => {
    const btn = document.getElementById('runBtn');
    if (btn) btn.click();
  });
  // Wait ~25s to capture mid-stream with reasoning/tools visible
  console.log('    waiting for streaming (25s)...');
  await new Promise(r => setTimeout(r, 25000));
});

// 3. Query result - wait for full completion, scroll to HCL output
await snap('03-query-result', async () => {
  console.log('    waiting for completion (100s)...');
  await new Promise(r => setTimeout(r, 100000));
  // Scroll to the agent-output div which contains the rendered HCL
  await page.evaluate(() => {
    const output = document.querySelector('.agent-output');
    if (output) output.scrollIntoView({ block: 'start' });
  });
  await new Promise(r => setTimeout(r, 500));
});

// 4. Knowledge Browser
await snap('04-knowledge', async () => {
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('nav button')].find(b => b.textContent.includes('Knowledge'));
    if (btn) btn.click();
  });
  await new Promise(r => setTimeout(r, 3000));
  // Click first module ref
  await page.evaluate(() => {
    const items = document.querySelectorAll('#knowledgeList .module-item, #knowledgeList > div > div');
    if (items.length > 0) items[0].click();
  });
  await new Promise(r => setTimeout(r, 2000));
});

// 5. Index Jobs
await snap('05-index-jobs', async () => {
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('nav button')].find(b => b.textContent.includes('Index Jobs'));
    if (btn) btn.click();
  });
  await new Promise(r => setTimeout(r, 3000));
});

// 6. Audit Logs
await snap('06-audit-logs', async () => {
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('nav button')].find(b => b.textContent.includes('Audit'));
    if (btn) btn.click();
  });
  await new Promise(r => setTimeout(r, 2000));
});

await browser.close();
console.log(`\nDone! Screenshots saved to ${OUT}/`);
