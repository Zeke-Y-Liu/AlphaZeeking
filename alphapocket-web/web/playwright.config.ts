import { defineConfig } from '@playwright/test';

/**
 * E2E 冒烟配置（RG-3，交互流程完整性 · Chrome 兼容基线）。
 * - 基线浏览器：Chromium（Chrome 内核），多浏览器矩阵为后续扩展；
 * - 前置：全栈已启动（web :5173 / bff :8081 / core :8082）；
 * - 长超时：登录/页面切换为主，不含计算任务。
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  // 全量回归顺序执行时 dev server/后端可能冷启（偶发首断言超时）→ 允许一次重试；
  // 连续两次失败才是真失败。
  retries: 1,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:5173',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'off',
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
});
