import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Portfolio, ReviewPayload, Run } from '../../api/types';
import PortfolioDetailView from '../PortfolioDetailView.vue';

vi.mock('../../api', () => ({
  fetchRunArtifact: vi.fn(),
  getJob: vi.fn(),
  getPortfolio: vi.fn(),
  listRuns: vi.fn(),
  reviewPortfolio: vi.fn(),
}));

import { fetchRunArtifact, getJob, getPortfolio, listRuns, reviewPortfolio } from '../../api';

const mGetPortfolio = vi.mocked(getPortfolio);
const mListRuns = vi.mocked(listRuns);
const mReview = vi.mocked(reviewPortfolio);
const mArtifact = vi.mocked(fetchRunArtifact);
const mGetJob = vi.mocked(getJob);

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
          qua_roe_ttm: 30,
          qua_margin: 91,
          grow_rev_yoy: 11,
          grow_np_yoy: 12,
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

  it('门禁未全绿 → 评估不阻断：202 data_risk → 琥珀警示条 + 数据页链接', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    const dataRisk = {
      status: 'fail' as const,
      message: '数据验证未全绿，评估结果可能失真；建议先完成数据同步并查看验证明细',
      failed: [{ check: 'golden_test', text: 'FAIL(510300.SH 缺失)' }],
      job_id: 'job_sync_x',
    };
    // 初始加载无 run；评估完成后 load() 拉到带 data_risk 的 run（与真实后端一致）
    mListRuns.mockResolvedValueOnce([]);
    mReview.mockResolvedValue({ job_id: 'job-risk', poll: '/api/v1/jobs/job-risk', data_risk: dataRisk });
    mGetJob.mockResolvedValue({ status: 'done', progress: 100 } as never);
    const riskyRun = makeRun();
    riskyRun.summary = { data_risk: dataRisk };
    mListRuns.mockResolvedValue([riskyRun]);

    const { wrapper } = await mountDetail();
    await wrapper.find('[data-test="evaluate-btn"]').trigger('click');
    await flushPromises();

    const bar = wrapper.find('[data-test="risk-bar"]');
    expect(bar.exists()).toBe(true);
    expect(bar.text()).toContain('golden_test');
    expect(bar.text()).toContain('评估结果可能失真');

    const link = wrapper.find('[data-test="risk-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toContain('数据');
  });

  it('runs.summary.data_risk 持久化 → 刷新后仍显示警示条', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mReview.mockResolvedValue({
      job_id: 'job-x',
      poll: '/api/v1/jobs/job-x',
    } as never);
    mArtifact.mockResolvedValue(makeReview());
    const riskyRun = makeRun();
    riskyRun.summary = {
      data_risk: {
        status: 'unknown',
        message: '尚未完成数据同步与验证，评估结果可能基于不完整数据',
        failed: [],
        job_id: null,
      },
    };
    mListRuns.mockResolvedValue([riskyRun]);

    const { wrapper } = await mountDetail();

    const bar = wrapper.find('[data-test="risk-bar"]');
    expect(bar.exists()).toBe(true);
    expect(bar.text()).toContain('不完整数据');
    // unknown（从未验证）≠ fail（有未通过项），无明细时兜底文案需区分
    expect(bar.text()).toContain('验证状态未知');
  });

  it('data_risk.status=fail 且无明细项 → 兜底文案「存在未通过项」', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    const riskyRun = makeRun();
    riskyRun.summary = {
      data_risk: {
        status: 'fail',
        message: '部分数据检查未通过，评估结果可能基于不完整数据',
        failed: [],
        job_id: null,
      },
    };
    mListRuns.mockResolvedValue([riskyRun]);

    const { wrapper } = await mountDetail();

    expect(wrapper.find('[data-test="risk-bar"]').text()).toContain('存在未通过项');
  });

  it('无 review run → 空态与「先运行评估」按钮', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([]);

    const { wrapper } = await mountDetail();

    expect(wrapper.find('[data-test="empty-state"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="empty-evaluate"]').text()).toContain('先运行评估');
  });

  // ── T-C7：引擎C 宏观流动性卡片（runs.summary.macro_liquidity）──
  it('引擎C 卡片渲染三市场徽章与全局指数', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    const run = makeRun();
    run.summary = {
      macro_liquidity: {
        markets: { A: 1, HK: -1, US: 0 },
        global: 0,
        global_score: 0.12,
        state: 'neutral',
        as_of: '2024-06-28',
      },
    };
    mListRuns.mockResolvedValue([run]);
    mArtifact.mockResolvedValue(makeReview());

    const { wrapper } = await mountDetail();

    const badgeA = wrapper.find('[data-test="macro-badge-A"]');
    expect(badgeA.exists()).toBe(true);
    expect(badgeA.text()).toContain('偏松');            // 正档位 → 偏松（tone=success）
    expect(wrapper.find('[data-test="macro-badge-HK"]').text()).toContain('偏紧');
    expect(wrapper.find('[data-test="macro-badge-US"]').text()).toContain('中性');
    const g = wrapper.find('[data-test="macro-global"]');
    expect(g.exists()).toBe(true);
    expect(g.text()).toContain('全局');
    expect(g.text()).toContain('+0.12');
    expect(wrapper.find('[data-test="macro-warn"]').exists()).toBe(false); // 中性态无警示
  });

  it('极端态提示：tight_stress → macro-warn 可见且含「紧缩」', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    const run = makeRun();
    run.summary = {
      macro_liquidity: {
        markets: { A: -2 },
        global: -2,
        global_score: -0.71,
        state: 'tight_stress',
        as_of: '2024-06-28',
      },
    };
    mListRuns.mockResolvedValue([run]);
    mArtifact.mockResolvedValue(makeReview());

    const { wrapper } = await mountDetail();

    const warn = wrapper.find('[data-test="macro-warn"]');
    expect(warn.exists()).toBe(true);
    expect(warn.text()).toContain('紧缩');
    expect(wrapper.find('[data-test="macro-badge-A"]').text()).toContain('深度紧缩');
  });

  it('unavailable 降级：macro-unavailable 可见、无徽章', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    const run = makeRun();
    run.summary = { macro_liquidity: { status: 'unavailable', error: '宏观流动性序列为空' } };
    mListRuns.mockResolvedValue([run]);
    mArtifact.mockResolvedValue(makeReview());

    const { wrapper } = await mountDetail();

    expect(wrapper.find('[data-test="macro-unavailable"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="macro-unavailable"]').text()).toContain('不影响其余评估');
    expect(wrapper.find('[data-test="macro-badge-A"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="macro-global"]').exists()).toBe(false);
  });

  // ── 引擎D 指数级别趋势卡片（runs.summary.index_trend，2026-10-26）──
  function makeIndexTrendSummary() {
    return {
      index_trend: {
        indexes: {
          ndx: { position: 'high_zone', pos_q: 88.5, trend: 'up', ret_20d: 0.041,
                 close: 26100.3, name: '纳斯达克综合', market: 'US' },
          csi300: { position: 'mid_zone', pos_q: 46.2, trend: 'range', ret_20d: -0.003,
                    close: 4095.1, name: '沪深300', market: 'A' },
          hstech: { position: 'low_zone', pos_q: 12.4, trend: 'down', ret_20d: -0.062,
                    close: 3888.1, name: '恒生科技', market: 'HK' },
          chinext: { position: 'mid_zone', pos_q: 55.0, trend: 'range', ret_20d: 0.008,
                     close: 2645.7, name: '创业板指', market: 'A' },
        },
        as_of: '2026-10-26',
      },
    };
  }

  it('引擎D 卡片渲染四指数：位置徽章 + 趋势徽章 + 位置条宽度', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    const run = makeRun();
    run.summary = makeIndexTrendSummary();
    mListRuns.mockResolvedValue([run]);
    mArtifact.mockResolvedValue(makeReview());

    const { wrapper } = await mountDetail();

    // 四指数按规范序渲染（ndx → csi300 → hstech → chinext）
    const pos = wrapper.findAll('[data-test^="it-pos-"]');
    expect(pos.map((b) => b.text())).toEqual([
      '高位区 89%', '中位区 46%', '低位区 12%', '中位区 55%',
    ]);
    expect(wrapper.findAll('[data-test^="it-trend-"]').map((b) => b.text()))
      .toEqual(['上行', '震荡', '下行', '震荡']);
    // 位置条宽度 = 位置百分位取整（ndx 88.5 → 89%）
    const bar = wrapper.find('[data-test="it-posbar-ndx"] > i');
    expect(bar.exists()).toBe(true);
    expect((bar.element as HTMLSpanElement).style.width).toBe('89%');
    // 20日收益与口径注记
    expect(wrapper.find('[data-test="it-ret20-hstech"]').text()).toContain('-6.2%');
    expect(wrapper.find('[data-test="index-trend-note"]').text()).toContain('不构成买卖信号');
    expect(wrapper.find('[data-test="index-trend-unavailable"]').exists()).toBe(false);
  });

  it('引擎D unavailable 降级：index-trend-unavailable 可见、无指数徽章', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    const run = makeRun();
    run.summary = { index_trend: { status: 'unavailable', error: '指数日线数据集为空' } };
    mListRuns.mockResolvedValue([run]);
    mArtifact.mockResolvedValue(makeReview());

    const { wrapper } = await mountDetail();

    expect(wrapper.find('[data-test="index-trend-unavailable"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="index-trend-unavailable"]').text()).toContain('不影响其余评估');
    expect(wrapper.find('[data-test="it-pos-ndx"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="it-trend-ndx"]').exists()).toBe(false);
  });

  it('旧 run 无 index_trend 键 → 「暂无指数趋势数据」空态（向后兼容）', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([makeRun()]);      // summary = {}（旧 run）
    mArtifact.mockResolvedValue(makeReview());

    const { wrapper } = await mountDetail();

    expect(wrapper.find('[data-test="index-trend-unavailable"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="it-pos-ndx"]').exists()).toBe(false);
    expect(wrapper.text()).toContain('暂无指数趋势数据');
  });
});
