import { defineConfig, devices } from '@playwright/test';

// T7.1 端到端冒烟：两剧本（A 新建 multi5 全流 / B seed cn4 回归）+ 409/429 分支，
// 全部走 fixtures/api-mock.ts 的确定性 mock（AP_E2E_FIXTURE 语义内建，不依赖真实后端/外网）。
// C6：禁用整页截图像素断言，断言以 DOM 文本为主。
//
// 运行方式：make test-e2e（Playwright 自动起 web dev server；浏览器经 fixtures/test.ts 复用 CDP Chrome）。
export default defineConfig({
  testDir: './specs',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? 'list' : 'html',
  // 组件统一用 data-test 标注（非 Playwright 默认的 data-testid）
  use: {
    baseURL: process.env.AP_E2E_BASE_URL ?? 'http://localhost:8080',
    testIdAttribute: 'data-test',
    // 固定 locale，保证 toLocaleString 千分位（如 -18,000）与日期断言稳定
    locale: 'en-US',
    trace: 'on-first-retry',
  },
  webServer: {
    command: 'pnpm -C ../web exec vite --port 8080 --strictPort',
    url: 'http://localhost:8080',
    reuseExistingServer: true,
    timeout: 120_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
