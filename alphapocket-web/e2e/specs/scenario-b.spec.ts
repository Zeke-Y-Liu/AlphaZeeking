/**
 * 剧本 B：seed cn4 回归（评估 + 回测），并校验组合列表最近评估摘要。
 * 同区间同口径（2021-01-04 → 2026-09-30，月度再平衡）。
 */
import { expect, test } from '../fixtures/test';

import { bootstrap } from './helpers';

test.describe('剧本 B · seed cn4 回归', () => {
  test('cn4 评估 + 回测（A3 口径）', async ({ page }) => {
    await bootstrap(page, 'cn4');

    /* 评估：4 只 A 股持仓 */
    await page.goto('/portfolios/cn4');
    await expect(page.getByTestId('empty-state')).toBeVisible();
    await page.getByTestId('evaluate-btn').click();

    await expect(page.getByTestId('metric-day')).toHaveText('0.4%');
    await expect(page.getByTestId('fa-table').locator('tbody tr')).toHaveCount(4);
    await expect(page.getByTestId('beta-card')).toHaveCount(4);
    await expect(page.getByTestId('next-trade-date')).toHaveText('2026-10-08');

    /* 组合列表：最近评估四指标来自最新 run（此时最新为 review）的 summary.portfolio_metrics */
    await page.goto('/portfolios');
    const card = page.locator('[data-test="pf-card"]', { hasText: 'cn4' });
    await expect(card.getByTestId('pf-metric-day')).toHaveText('0.4%');
    await expect(card.getByTestId('pf-metric-ytd')).toHaveText('8.7%');
    await expect(card.getByTestId('pf-metric-1y')).toHaveText('14.2%');

    /* 回测：cn4 累计 +45.2% */
    await page.goto('/portfolios/cn4/backtest');
    await expect(page.getByTestId('bt-empty')).toBeVisible();
    await page.getByTestId('bt-empty-run').click();

    await expect(page.getByTestId('bt-total-return')).toHaveText('45.2%');
    await expect(page.getByTestId('bt-n-days')).toHaveText('1393');
    await expect(page.getByTestId('bt-n-rebalances')).toHaveText('69');
  });
});
