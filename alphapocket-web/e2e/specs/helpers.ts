/**
 * E2E 公共辅助：mock 安装 + 登录态预置。
 *
 * 登录态直接写入 localStorage（键与 stores/auth.ts 一致），跳过登录页；
 * 登录相关的 401/429 分支另有专测，不走此辅助。
 */
import type { Page } from '@playwright/test';

import { createStore, installApiMocks } from '../fixtures/api-mock';
import type { MockStore, Scenario } from '../fixtures/api-mock';

/** 预置有效凭证（router guard 的 auth.restore() 会读取）。 */
export async function seedAuth(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('ap.access', 'e2e-access');
    localStorage.setItem('ap.refresh', 'e2e-refresh');
    localStorage.setItem('ap.user', JSON.stringify({ id: 1, username: 'z', role: 'owner' }));
  });
}

/** 安装确定性 API mock + 预置登录态；返回有状态 store（供分支用例改写开关）。 */
export async function bootstrap(page: Page, scenario: Scenario): Promise<MockStore> {
  const store = createStore(scenario);
  await installApiMocks(page, store);
  await seedAuth(page);
  return store;
}
