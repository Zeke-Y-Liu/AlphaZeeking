import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/types';
import type { Portfolio } from '../../api/types';
import PortfoliosView from '../PortfoliosView.vue';

vi.mock('../../api', () => ({
  listPortfolios: vi.fn(),
}));

import { listPortfolios } from '../../api';

const mockedList = vi.mocked(listPortfolios);

const Stub = { template: '<div />' };

async function mountList(): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const routes: RouteRecordRaw[] = [
    { path: '/portfolios', name: 'portfolios', component: Stub },
    { path: '/portfolios/new', name: 'portfolio-new', component: Stub },
    { path: '/portfolios/:id', name: 'portfolio-detail', component: Stub },
  ];
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push('/portfolios');
  await router.isReady();
  const wrapper = mount(PortfoliosView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}

function makePortfolio(over: Partial<Portfolio>): Portfolio {
  return {
    id: 1,
    pid: 'multi5',
    name: '测试组合',
    base_ccy: 'CNY',
    strategy: {},
    status: 'active',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    position_count: 1,
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('PortfoliosView', () => {
  it('渲染卡片：状态徽章、标的徽章、四指标（缺失显示 —）', async () => {
    mockedList.mockResolvedValue([
      makePortfolio({
        pid: 'multi5',
        status: 'active',
        positions: [
          { secid: '510300.SH', weight: 20, name: '沪深300ETF', asset_type: 'ETF' },
        ],
        latest_run: {
          run_uid: 'run-a',
          type: 'review',
          as_of: '2024-06-28',
          summary: {
            portfolio_metrics: {
              day_ret: 0.0123,
              ret_ytd: null,
              ret_1y: 0.05,
              max_dd_1y: -0.2,
            },
          },
        },
      }),
      makePortfolio({
        id: 2,
        pid: 'old1',
        name: '归档组合',
        status: 'archived',
        position_count: 0,
        positions: [],
        latest_run: null,
      }),
    ]);

    const { wrapper } = await mountList();

    const cards = wrapper.findAll('[data-test="pf-card"]');
    expect(cards).toHaveLength(2);

    const statuses = wrapper.findAll('[data-test="pf-status"]');
    expect(statuses[0].text()).toBe('active');
    expect(statuses[1].text()).toBe('archived');

    // 标的徽章行（名称 + asset_type）
    expect(wrapper.text()).toContain('沪深300ETF');
    expect(wrapper.text()).toContain('ETF');

    // 四指标：day_ret=1.2%，ytd 缺失 → —，1y=5.0%，MDD=-20.0%
    const card0 = cards[0];
    expect(card0.find('[data-test="pf-metric-day"]').text()).toBe('1.2%');
    expect(card0.find('[data-test="pf-metric-ytd"]').text()).toBe('—');
    expect(card0.find('[data-test="pf-metric-1y"]').text()).toBe('5.0%');
    expect(card0.find('[data-test="pf-metric-mdd"]').text()).toBe('-20.0%');

    // 最近 run 时间
    expect(card0.find('[data-test="pf-run-time"]').text()).toContain('2024-06-28');

    // 归档且无 run → 时间与指标均为 —
    const card1 = cards[1];
    expect(card1.find('[data-test="pf-run-time"]').text()).toContain('—');
    expect(card1.find('[data-test="pf-metric-day"]').text()).toBe('—');
  });

  it('空列表显示空态', async () => {
    mockedList.mockResolvedValue([]);
    const { wrapper } = await mountList();
    expect(wrapper.find('[data-test="pf-empty"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-test="pf-card"]')).toHaveLength(0);
  });

  it('加载失败显示错误态并可重试', async () => {
    mockedList.mockRejectedValueOnce(new ApiError('INTERNAL_ERROR', '服务异常', 500));
    const { wrapper } = await mountList();

    expect(wrapper.find('[data-test="pf-error"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pf-error"]').text()).toContain('服务异常');

    mockedList.mockResolvedValueOnce([makePortfolio({})]);
    await wrapper.find('[data-test="pf-retry"]').trigger('click');
    await flushPromises();
    expect(wrapper.findAll('[data-test="pf-card"]')).toHaveLength(1);
  });
});
