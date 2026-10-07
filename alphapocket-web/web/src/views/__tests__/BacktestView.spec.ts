import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/types';
import type { BacktestPayload, DataStatus, Portfolio, Run } from '../../api/types';
import BacktestView from '../BacktestView.vue';

vi.mock('../../api', () => ({
  backtestPortfolio: vi.fn(),
  fetchRunArtifact: vi.fn(),
  getDataStatus: vi.fn(),
  getJob: vi.fn(),
  getPortfolio: vi.fn(),
  listRuns: vi.fn(),
}));

// ECharts 在 jsdom 下无画布尺寸，mock 掉避免报错；页面图表属增强，不应影响断言。
vi.mock('../../utils/echarts', () => ({
  default: {
    init: () => ({ setOption() {}, dispose() {}, resize() {} }),
  },
}));

import {
  backtestPortfolio,
  fetchRunArtifact,
  getDataStatus,
  getPortfolio,
  listRuns,
} from '../../api';

const mGetPortfolio = vi.mocked(getPortfolio);
const mListRuns = vi.mocked(listRuns);
const mArtifact = vi.mocked(fetchRunArtifact);
const mDataStatus = vi.mocked(getDataStatus);
const mBacktest = vi.mocked(backtestPortfolio);

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

function makeRun(over: Partial<Run> = {}): Run {
  return {
    id: 9,
    run_uid: 'bt-run-0001234567',
    job_id: 'job-bt',
    portfolio_id: 1,
    type: 'backtest',
    as_of: '2024-06-28',
    summary: { n_rebalances: 0, n_days: 1000 },
    artifact_keys: ['backtest.json'],
    created_at: '2024-06-28T00:00:00Z',
    ...over,
  };
}

function makePayload(over: Partial<BacktestPayload> = {}): BacktestPayload {
  return {
    portfolio: 'multi5',
    portfolio_name: '测试组合',
    strategy: '等权·买入持有',
    rebalance: 'buyhold',
    benchmark_secid: '510300.SH',
    start: '2020-01-01',
    end: '2024-06-28',
    n_days: 1000,
    n_rebalances: 0,
    cost_bps: 2.5,
    rf: 0,
    series: [
      ['2020-01-01', 1],
      ['2024-06-28', 2.056],
    ],
    benchmark: {
      secid: '510300.SH',
      name: '沪深300ETF',
      metrics: {
        total_return: 0.5,
        cagr: 0.1,
        ann_vol: 0.2,
        sharpe: 0.4,
        sortino: 0.5,
        max_dd: -0.35,
        calmar: 0.28,
        win_rate: 0.52,
      },
      series: [
        ['2020-01-01', 1],
        ['2024-06-28', 1.5],
      ],
    },
    strategy_metrics: {
      total_return: 1.056,
      cagr: 0.2,
      ann_vol: 0.25,
      sharpe: 0.8,
      sortino: 1.0,
      max_dd: -0.3,
      calmar: 0.66,
      win_rate: 0.55,
    },
    excess: { total: 0.556, cagr: 0.1 },
    monthly: { '2020-01': 0.01 },
    yearly: { '2020': { '01': 0.01, Y: 0.01 } },
    per_asset: [
      {
        secid: '510300.SH',
        name: '沪深300ETF',
        weight: 0.5,
        total_return: 0.5,
        cagr: 0.1,
        ann_vol: 0.2,
        max_dd: -0.35,
        corr: 0.9,
      },
    ],
    cost: { n: 0, total: 0, avg_turnover: 0 },
    ...over,
  };
}

function makeStatus(): DataStatus {
  return {
    as_of: '2024-06-28',
    universe: { count: 1, types: { STOCK: 1, ETF: 0, LOF: 0 } },
    securities: [
      {
        secid: '600519.SH',
        code: '600519',
        name: '贵州茅台',
        market: 'A',
        asset_type: 'STOCK',
        last_bar: '2024-06-28',
        lag_days: 0,
        missing: 0,
        has_bars: true,
      },
    ],
    verification: { status: 'pass', job_id: null, created_at: null, checks: [], failed: [] },
    latest_sync: null,
  };
}

async function mountBacktest(): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const routes: RouteRecordRaw[] = [
    { path: '/portfolios/:id', name: 'portfolio-detail', component: Stub },
    { path: '/portfolios/:id/backtest', name: 'portfolio-backtest', component: Stub },
    { path: '/data', name: 'data', component: Stub },
  ];
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push('/portfolios/1/backtest');
  await router.isReady();
  const wrapper = mount(BacktestView, { global: { plugins: [router] } });
  await flushPromises();
  await flushPromises();
  return { wrapper, router };
}

beforeEach(() => {
  vi.clearAllMocks();
  mDataStatus.mockResolvedValue(makeStatus());
});

describe('BacktestView', () => {
  it('无回测记录 → 参数表单 + 空态（默认月度 / 基准 510300.SH）', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([]);

    const { wrapper } = await mountBacktest();

    // 参数表单五项
    expect(wrapper.find('[data-test="bt-start"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="bt-end"]').exists()).toBe(true);

    const rebalance = wrapper.find('[data-test="bt-rebalance"]');
    expect(rebalance.exists()).toBe(true);
    expect(rebalance.element).toHaveProperty('value', 'monthly');
    expect(rebalance.findAll('option').map((o) => o.element.value)).toEqual([
      'daily',
      'monthly',
      'quarterly',
      'yearly',
      'buyhold',
    ]);

    const benchmark = wrapper.find('[data-test="bt-benchmark"]');
    expect(benchmark.element).toHaveProperty('value', '510300.SH');
    expect(wrapper.find('[data-test="bt-rf"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="bt-run"]').exists()).toBe(true);

    // 空态与立即回测按钮
    expect(wrapper.find('[data-test="bt-empty"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="bt-empty-run"]').text()).toContain('立即回测');

    expect(mListRuns).toHaveBeenCalledWith({ type: 'backtest', portfolio_id: 1, limit: 20 });
  });

  it('加载最近一次回测：净值双线表 + 累计收益格 / 再平衡计数（buyhold=0）', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([makeRun()]);
    mArtifact.mockResolvedValue(makePayload());

    const { wrapper } = await mountBacktest();

    expect(mArtifact).toHaveBeenCalledWith('bt-run-0001234567', 'backtest.json', 'json');

    // 八指标对比表 + 关键单元格
    expect(wrapper.find('[data-test="bt-metrics-table"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="bt-total-return"]').text()).toBe('105.6%');
    expect(wrapper.find('[data-test="bt-n-days"]').text()).toBe('1000');
    expect(wrapper.find('[data-test="bt-n-rebalances"]').text()).toBe('0');

    // 月度 / 标的贡献 / 图表容器
    expect(wrapper.find('[data-test="bt-monthly"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="bt-per-asset"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="bt-chart"]').exists()).toBe(true);

    // 历史回测列表项
    expect(wrapper.findAll('[data-test="bt-history-item"]')).toHaveLength(1);
  });

  it('提交 409 VERIFY_GATE_FAILED → 门禁红条 + 数据页链接', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([]);
    mBacktest.mockRejectedValue(
      new ApiError('VERIFY_GATE_FAILED', '数据验证未全绿', 409, { failed: ['golden_test'] }),
    );

    const { wrapper } = await mountBacktest();
    await wrapper.find('[data-test="bt-run"]').trigger('click');
    await flushPromises();

    const gate = wrapper.find('[data-test="bt-gate"]');
    expect(gate.exists()).toBe(true);
    expect(gate.text()).toContain('golden_test');
    expect(wrapper.find('[data-test="bt-gate-link"]').exists()).toBe(true);
  });

  it('提交 422 → toast.error（不渲染门禁红条）', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([]);
    mBacktest.mockRejectedValue(new ApiError('VALIDATION_ERROR', 'start 不得晚于 end', 422));

    const { wrapper } = await mountBacktest();
    await wrapper.find('[data-test="bt-run"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="bt-gate"]').exists()).toBe(false);
  });
});
