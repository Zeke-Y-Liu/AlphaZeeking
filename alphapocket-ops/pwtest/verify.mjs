// 桌面端登录 e2e：gh-pages 登录 → #/portfolios → cn4/multi5 渲染 + 零 console error
import { chromium } from 'playwright';

const URL = 'https://zeke-y-liu.github.io/AlphaZeeking/';
const consoleErrors = [];
const pageErrors = [];

const browser = await chromium.launch({
  args: ['--proxy-server=http://127.0.0.1:18080', '--no-sandbox'],
});
try {
  const page = await browser.newPage();
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => pageErrors.push(String(err)));

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForSelector('[data-test="username"]', { timeout: 60000 });
  console.log('[1] login form visible');

  await page.fill('[data-test="username"]', 'z');
  await page.fill('[data-test="password"]', 'Alpha618');
  await page.click('[data-test="submit"]');
  console.log('[2] credentials submitted');

  await page.waitForURL(/#\/?portfolios/, { timeout: 60000 });
  console.log('[3] redirected to portfolios:', page.url());

  await page.waitForFunction(
    () => /cn4/i.test(document.body.innerText) && /multi5/i.test(document.body.innerText),
    { timeout: 60000 },
  );
  const body = await page.evaluate(() => document.body.innerText);
  console.log('[4] cn4 rendered:', /cn4/i.test(body), '| multi5 rendered:', /multi5/i.test(body));

  console.log('consoleErrors:', consoleErrors.length, JSON.stringify(consoleErrors));
  console.log('pageErrors:', pageErrors.length, JSON.stringify(pageErrors));
  if (consoleErrors.length || pageErrors.length) throw new Error('browser errors captured');

  console.log('E2E PASS');
} finally {
  await browser.close();
}
