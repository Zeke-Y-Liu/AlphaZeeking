/**
 * E2E test 基座（T7.1）。
 *
 * 沙箱内无 ms-playwright 浏览器，但存在已运行的 Chrome（CDP 9222）。因此默认经
 * `connectOverCDP` 复用该浏览器：本地无浏览器也能跑；本地有浏览器时可设
 * `AP_E2E_CDP=launch` 强制 Playwright 自行启动 chromium。
 *
 * 注意：经 CDP 复用的浏览器归用户所有，测试结束**不关闭**它。
 */
import { test as base, chromium } from '@playwright/test';
import type { Browser } from '@playwright/test';

const DEFAULT_CDP = 'http://127.0.0.1:9222';

export const test = base.extend<Record<string, never>, { browser: Browser }>({
  browser: async ({}, use) => {
    const endpoint = process.env.AP_E2E_CDP ?? DEFAULT_CDP;
    if (endpoint !== 'launch') {
      try {
        const browser = await chromium.connectOverCDP(endpoint, { timeout: 10_000 });
        await use(browser);
        return; // 复用既有浏览器，不关闭
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn(`[e2e] CDP 连接失败（${endpoint}），回退本地启动 chromium：${String(err)}`);
      }
    }
    const browser = await chromium.launch();
    await use(browser);
    await browser.close();
  },
});

export { expect } from '@playwright/test';
