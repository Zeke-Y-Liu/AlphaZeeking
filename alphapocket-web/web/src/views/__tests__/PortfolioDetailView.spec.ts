import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/types';
import type { Portfolio, ReviewPayload, Run } from '../../api/types';
import PortfolioDetailView from '../PortfolioDetailView.vue';

vi.mock('../../api', () => ({
  fetchRunArtifact: vi.fn(),
  getJob: vi.fn(),
  getPortfolio: vi.fn(),
  listRuns: vi.fn(),
  reviewPortfolio: vi.fn(),
}));

import { fetchRunArtifact, getPortfolio, listRuns, reviewPortfolio } from '../../api';

const mGetPortfolio = vi.mocked(getPortfolio);
const mListRuns = vi.mocked(listRuns);
const mReview = vi.mocked(reviewPortfolio);
const mArtifact = vi.mocked(fetchRunArtifact);

const Stub = { template: '<div />' };

function makePortfolio(over: Partial<Portfolio> = {}): Portfolio {
  return {
    id: 1,
    pid: 'multi5',
    name: '测试组合',
    base_ccy: 'CNY',
    strategy: {},
    status: 'active',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    position_count: 2,
    ...over,
  };
}

function makeRun(): Run {
  return {
    id: 9,
    run_uid: 'run-x',
    job_id: 'job-x',
    portfolio_id: 1,
    type: 'review',
    as_of: '2024-06-28',
    summary: {},
    artifact_keys: ['review.json'],
    created_at: '2024-06-28T00:00:00Z',
  };
}

function makeReview(): ReviewPayload {
  return {
    id: 1,
    name: '测试组合',
    as_of: '2024-06-28',
    base_ccy: 'CNY',
    next_trade_date: '2024-07-01',
    holiday_days: 2,
    portfolio_metrics: {
      day_ret: 0.0123,
      ret_20d: -0.02,
      ret_ytd: 0.05,
      ret_1y: 0.08,
      max_dd_1y: -0.2,
      nav: 1.1,
      method: 'TWR',
    },
    positions: [
      {
        // 基金/指数类：无财报 → alpha 估值/质量/成长字段为 null → 前端显示 —
        secid: '510300.SH',
        name: '沪深300ETF',
        asset_type: 'ETF',
        weight: 60,
        beta: { state: 'above', mom_20d: 0.03, ma20_break: false, high20_touch: true },
        alpha: { val_pe_ttm: null, val_pb: null, qua_roe_ttm: null, qua_margin: null },
      },
      {
        secid: '600519.SH',
        name: '贵州茅台',
        asset_type: 'STOCK',
        weight: 40,
        beta: { state: 'below', mom_20d: -0.04, ma20_break: true, high20_touch: false },
        alpha: {
          val_pe_ttm: 25.1,
          val_pb: 8.2,
          qua_roe_ttm: 0.3,
          qua_margin: 0.91,
          grow_rev_yoy: 0.11,
          grow_np_yoy: 0.12,
          mom_12_1: 0.05,
          vol_250: 0.22,
          div_yield_ttm: 0.025,
          used_period: '2024Q1',
        },
      },
    ],
    signals: [
      {
        secid: '510300.SH',
        name: '沪深300ETF',
        signal_id: 's1',
        type: 'ma20',
        action: '减仓 40%',
        cooldown: true,
      },
    ],
    data_quality: { last_bar: '2024-06-28' },
  };
}

async function mountDetail(
  path = '/portfolios/1',
): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const routes: RouteRecordRaw[] = [
    { path: '/portfolios', name: 'portfolios', component: Stub },
    { path: '/portfolios/:id', name: 'portfolio-detail', component: Stub },
    { path: '/portfolios/:id/report', name: 'portfolio-report', component: Stub },
    { path: '/data', name: 'data', component: Stub },
  ];
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(PortfolioDetailView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('PortfolioDetailView', () => {
  it('概览渲染：五项体检 / 引擎A（ETF 估值为 —）/ 引擎B 徽章 / 下一交易日 / 数据截至', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([makeRun()]);
    mArtifact.mockResolvedValue(makeReview());

    const { wrapper } = await mountDetail();

    // 体检卡五指标
    expect(wrapper.find('[data-test="metric-day"]').text()).toBe('1.2%');
    expect(wrapper.find('[data-test="metric-20d"]').text()).toBe('-2.0%');
    expect(wrapper.find('[data-test="metric-ytd"]').text()).toBe('5.0%');
    expect(wrapper.find('[data-test="metric-1y"]').text()).toBe('8.0%');
    expect(wrapper.find('[data-test="metric-mdd"]').text()).toBe('-20.0%');

    // 引擎A：ETF 行估值列显示 —（不显示 0/NaN），个股正常显示
    expect(wrapper.find('[data-test="fa-name-510300.SH"]').text()).toBe('沪深300ETF');
    expect(wrapper.find('[data-test="fa-pe-510300.SH"]').text()).toBe('—');
    expect(wrapper.find('[data-test="fa-pe-600519.SH"]').text()).toBe('25.10');
    expect(wrapper.find('[data-test="a-stale"]').text()).toContain('2024Q1');

    // 引擎B：state 徽章文案 + 冷却提示
    const states = wrapper.findAll('[data-test="beta-state"]');
    expect(states.map((s) => s.text())).toEqual(['above', 'below']);
    expect(wrapper.find('[data-test="cooldown"]').exists()).toBe(true);

    // 下一交易日指引
    expect(wrapper.find('[data-test="next-trade-date"]').text()).toBe('2024-07-01');
    expect(wrapper.find('[data-test="holiday-days"]').text()).toBe('2');
    expect(wrapper.find('[data-test="actions"]').text()).toContain('减仓 40%');

    // 数据截至徽章
    expect(wrapper.find('[data-test="asof-badge"]').text()).toContain('2024-06-28');

    // 数据源：最近一次 review run + review.json 产物
    expect(mListRuns).toHaveBeenCalledWith({ type: 'review', portfolio_id: 1, limit: 1 });
    expect(mArtifact).toHaveBeenCalledWith('run-x', 'review.json', 'json');
  });

  it('门禁 409 VERIFY_GATE_FAILED → 红条 + 数据页链接', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([]);
    mReview.mockRejectedValue(
      new ApiError('VERIFY_GATE_FAILED', '数据验证未全绿', 409, { failed: ['golden_test'] }),
    );

    const { wrapper } = await mountDetail();
    await wrapper.find('[data-test="evaluate-btn"]').trigger('click');
    await flushPromises();

    const bar = wrapper.find('[data-test="gate-bar"]');
    expect(bar.exists()).toBe(true);
    expect(bar.text()).toContain('golden_test');

    const link = wrapper.find('[data-test="gate-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toContain('数据');
  });

  it('无 review run → 空态与「先运行评估」按钮', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([]);

    const { wrapper } = await mountDetail();

    expect(wrapper.find('[data-test="empty-state"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="empty-evaluate"]').text()).toContain('先运行评估');
  });
});
