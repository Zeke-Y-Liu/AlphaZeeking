/**
 * 剧本 A：新建 multi5（新 workspace）→ 评估 → 回测（A2 锚点）→ 调仓 → 执行单
 * → 纪律确认 → 登记成交，并在数据页做一次增量同步。
 *
 * 全程走确定性 mock（fixtures/api-mock.ts），断言 α 锚点数值。
 */
import { expect, test } from '../fixtures/test';

import { bootstrap } from './helpers';

/** multi5 五标的代码（对齐种子与 mock 的 MULTI5）。 */
const MULTI5_CODES = ['510300', '501018', '518880', '513100', '511260'];

test.describe('剧本 A · multi5 全流', () => {
  test('新建 → 评估 → 回测 → 调仓 → 执行单 → 确认 → 登记', async ({ page }) => {
    await bootstrap(page, 'multi5');

    /* ① 新建组合向导：基本信息 → 标的与权重 → 确认 */
    await page.goto('/portfolios/new');
    await page.getByTestId('name').fill('multi5 组合');
    await page.getByTestId('pid').fill('multi5');
    await page.getByTestId('to-positions').click();

    for (const code of MULTI5_CODES) {
      await page.getByTestId('search-input').fill(code);
      const row = page.locator('.np-result', { hasText: code });
      await expect(row).toHaveCount(1);
      await expect(row.getByTestId('search-result')).toBeEnabled();
      await row.getByTestId('search-result').click();
    }
    await expect(page.getByTestId('position-list').locator('.np-pos')).toHaveCount(5);

    await page.getByTestId('equal-weight').click();
    await expect(page.getByTestId('weight-sum')).toHaveText('合计 100.0%');
    await page.getByTestId('to-confirm').click();
    await expect(page.getByTestId('step-confirm')).toBeVisible();
    await page.getByTestId('submit').click();

    await expect(page).toHaveURL(/\/portfolios\/multi5$/);

    /* ② 评估：无 run → 空态；运行后体检卡/因子表/信号卡/下一交易日指引就位 */
    await expect(page.getByTestId('empty-state')).toBeVisible();
    await page.getByTestId('evaluate-btn').click();

    await expect(page.getByTestId('metric-day')).toHaveText('0.4%');
    await expect(page.getByTestId('metric-ytd')).toHaveText('8.7%');
    await expect(page.getByTestId('asof-badge')).toContainText('2026-09-30');
    await expect(page.getByTestId('method')).toContainText('价格收益');
    await expect(page.getByTestId('fa-table').locator('tbody tr')).toHaveCount(5);
    await expect(page.getByTestId('beta-card')).toHaveCount(5);
    await expect(page.getByTestId('next-trade-date')).toHaveText('2026-10-08');
    await expect(page.getByTestId('holiday-days')).toHaveText('3');
    await expect(page.getByTestId('actions').locator('li')).toHaveCount(2);

    /* 引擎C 卡片（T-C7）：正常态温度计——全局档位/score/三市场档位/注记 */
    await expect(page.getByTestId('macro-card')).toBeVisible();
    await expect(page.getByTestId('macro-global')).toHaveText('偏松');
    await expect(page.getByTestId('macro-score')).toHaveText('0.458');
    await expect(page.getByTestId('macro-market-HK')).toHaveText('港股 中性');
    await expect(page.getByTestId('macro-asof')).toContainText('2026-09-30');
    await expect(page.getByTestId('macro-card').locator('..')).toContainText('不参与决策');

    /* ③ 回测：A2 锚点（累计 +105.6% / 1393 日 / 69 次再平衡） */
    await page.goto('/portfolios/multi5/backtest');
    await expect(page.getByTestId('bt-empty')).toBeVisible();
    await page.getByTestId('bt-empty-run').click();

    await expect(page.getByTestId('bt-total-return')).toHaveText('105.6%');
    await expect(page.getByTestId('bt-n-days')).toHaveText('1393');
    await expect(page.getByTestId('bt-n-rebalances')).toHaveText('69');
    await expect(page.getByTestId('bt-history-item')).toHaveCount(1);
    await expect(page.getByTestId('bt-chart')).toBeVisible();

    /* ④ 调仓：重跑生成 plan → 生成执行单（A4 锚点）→ 确认 → 登记成交 */
    await page.goto('/portfolios/multi5/rebalance');
    await expect(page.getByTestId('plan-empty')).toBeVisible();

    await page.getByTestId('rerun-plan').click();
    await expect(page.getByTestId('plan-uid')).toHaveText('RP-20260930-MULTI5');
    await expect(page.getByTestId('plan-trade')).toHaveCount(2);
    await expect(page.getByTestId('qdii-warn')).toBeVisible();

    await page.getByTestId('gen-exec').click();
    await expect(page.getByTestId('exec-status')).toHaveText('待确认');
    await expect(page.getByTestId('n-orders')).toHaveText('2');
    await expect(page.getByTestId('order-shares-510300.SH')).toHaveText('-18,000');
    await expect(page.getByTestId('order-shares-518880.SH')).toHaveText('-9,300');
    await expect(page.getByTestId('total-fee')).toHaveText('40.01');

    await page.getByTestId('ack-check').check();
    await page.getByTestId('confirm-btn').click();
    await expect(page.getByTestId('confirm-modal')).toBeVisible();
    await page.getByTestId('confirm-yes').click();
    await expect(page.getByTestId('exec-status')).toHaveText('已确认');

    await expect(page.getByTestId('fill-flow')).toBeVisible();
    await page.getByTestId('submit-fills').click();
    await expect(page.getByTestId('exec-status')).toHaveText('已成交');
    await expect(page.getByTestId('fills-summary')).toBeVisible();
    await expect(page.getByTestId('timeline-action-create')).toBeVisible();
    await expect(page.getByTestId('timeline-action-confirm')).toBeVisible();
    await expect(page.getByTestId('timeline-action-fill')).toBeVisible();

    /* ⑤ 数据页：六项验证全绿（A1 锚点）→ 验证明细 → 增量同步推进数据截至日 */
    await page.goto('/data');
    await expect(page.getByTestId('universe-count')).toHaveText('12');
    await expect(page.getByTestId('verify-summary')).toHaveText('pass');
    await expect(page.getByTestId('exempt-row')).toContainText('3');
    await expect(page.getByTestId('missing-row')).toContainText('0');
    await expect(page.getByTestId('verify-golden_test')).toContainText('pass');

    await page.getByTestId('verify-golden_test').click();
    await expect(page.getByTestId('verify-detail-text')).toHaveText('golden 99 项对账全绿');
    await page.getByTestId('verify-close').click();

    await page.getByTestId('sync-incremental').click();
    await expect(page.getByTestId('asof-badge')).toHaveText('2026-10-05');
    await expect(page.getByTestId('runs-list')).toBeVisible();
  });
});
