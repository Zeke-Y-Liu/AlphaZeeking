/**
 * 分支用例：门禁 409 VERIFY_GATE_FAILED、互斥 409 JOB_LOCKED、限流 429 ACCOUNT_LOCKED、401。
 */
import { createStore, installApiMocks } from '../fixtures/api-mock';
import { expect, test } from '../fixtures/test';

import { bootstrap } from './helpers';

test.describe('分支 · 门禁 / 互斥 / 限流', () => {
  test('409 VERIFY_GATE_FAILED：评估被门禁拦截并显示红条', async ({ page }) => {
    const store = await bootstrap(page, 'cn4');
    await page.goto('/portfolios/cn4');
    await expect(page.getByTestId('empty-state')).toBeVisible();

    store.gateFail = true;
    await page.getByTestId('evaluate-btn').click();

    await expect(page.getByTestId('gate-bar')).toBeVisible();
    await expect(page.getByTestId('gate-bar')).toContainText('golden_test');
    await expect(page.getByTestId('gate-link')).toBeVisible();
  });

  test('409 VERIFY_GATE_FAILED：回测被门禁拦截', async ({ page }) => {
    const store = await bootstrap(page, 'cn4');
    await page.goto('/portfolios/cn4/backtest');
    await expect(page.getByTestId('bt-empty')).toBeVisible();

    store.gateFail = true;
    await page.getByTestId('bt-empty-run').click();

    await expect(page.getByTestId('bt-gate')).toBeVisible();
    await expect(page.getByTestId('bt-gate')).toContainText('adjust_check');
  });

  test('409 JOB_LOCKED：评估排队提示', async ({ page }) => {
    const store = await bootstrap(page, 'cn4');
    await page.goto('/portfolios/cn4');
    await expect(page.getByTestId('empty-state')).toBeVisible();

    store.jobLocked = true;
    await page.getByTestId('evaluate-btn').click();

    await expect(page.getByTestId('queued-hint')).toBeVisible();
  });

  test('429 ACCOUNT_LOCKED：登录被限流锁定', async ({ page }) => {
    const store = createStore('cn4');
    await installApiMocks(page, store);
    store.loginLocked = true;

    await page.goto('/login');
    await page.getByTestId('username').fill('demo');
    await page.getByTestId('password').fill('alphapocket');
    await page.getByTestId('submit').click();

    await expect(page.getByTestId('locked')).toContainText('锁定');
  });

  test('401：密码错误内联提示', async ({ page }) => {
    const store = createStore('cn4');
    await installApiMocks(page, store);

    await page.goto('/login');
    await page.getByTestId('username').fill('demo');
    await page.getByTestId('password').fill('wrong-password');
    await page.getByTestId('submit').click();

    await expect(page.getByTestId('error')).toHaveText('用户名或密码错误');
  });
});
