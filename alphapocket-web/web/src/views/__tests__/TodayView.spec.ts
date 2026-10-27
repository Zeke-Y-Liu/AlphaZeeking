import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { TodayPortfolioItem, TodayPayload } from '../../api/types';
import TodayView from '../TodayView.vue';

vi.mock('../../api', () => ({
  getToday: vi.fn(),
}));

import { getToday } from '../../api';

const mockedGet = vi.mocked(getToday);

const Stub = { template: '<div />' };

async function mountView(): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const routes: RouteRecordRaw[] = [
    { path: '/today', name: 'today', component: Stub },
    { path: '/portfolios/:id', name: 'portfolio-detail', component: Stub },
  ];
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push('/today');
  await router.isReady();
  const wrapper = mount(TodayView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}

function makeItem(over: Partial<TodayPortfolioItem>): TodayPortfolioItem {
  return {
    portfolio_id: 1,
    pid: 'cn4',
    name: 'A股精选',
    run_uid: 'run_20261026_x',
    as_of: '2026-10-26',
    created_at: '2026-10-26T16:40:00',
    fresh: true,
    position_count: 3,
    metrics: { day_ret: 0.012, ret_20d: 0.05 } as TodayPortfolioItem['metrics'],
    signal_states: { above: 2, below: 1, neutral: 0 },
    highlights: ['今日 +1.20%，近20日 +5.00%', '信号偏多（2 上 / 1 下 / 0 中性），持仓可继续持有'],
    level: 'good',
    ...over,
  };
}

function makePayload(over: Partial<TodayPayload>): TodayPayload {
  return {
    server_time: '2026-10-27T09:30:01',
    date: '2026-10-27',
    generated_at: '2026-10-27T09:30:01',
    auto_review: { enabled: false },
    portfolios: [makeItem({})],
    ...over,
  };
}

describe('TodayView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('加载 today 并渲染：时钟/服务器时间/组合卡片 highlight 与徽章', async () => {
    mockedGet.mockResolvedValue(makePayload({}));
    const { wrapper } = await mountView();
    expect(mockedGet).toHaveBeenCalledTimes(1);
    // 当前时间：本地时钟 + 服务器时间 + 日期
    expect(wrapper.find('.today-clock').text()).toMatch(/\d{2}:\d{2}:\d{2}/);
    expect(wrapper.text()).toContain('2026-10-27');
    expect(wrapper.text()).toContain('服务器时间 2026-10-27 09:30:01');
    expect(wrapper.text()).toContain('更新于 2026-10-27 09:30:01');
    // 组合卡片：名称/pid/highlight 短句/fresh 提示/收益行
    expect(wrapper.text()).toContain('A股精选');
    expect(wrapper.text()).toContain('cn4');
    expect(wrapper.text()).toContain('信号偏多（2 上 / 1 下 / 0 中性），持仓可继续持有');
    expect(wrapper.text()).toContain('今日结论（as_of 2026-10-26）');
    expect(wrapper.text()).toContain('今日 1.20%');
    expect(wrapper.text()).toContain('持仓 3');
  });

  it('自动评估开启：横幅展示触发时点与今日进度', async () => {
    mockedGet.mockResolvedValue(
      makePayload({
        auto_review: {
          enabled: true,
          fire_time: '16:30',
          sync: 'done',
          review_submitted: 2,
          review_done: 2,
          review_failed: 0,
        },
      }),
    );
    const { wrapper } = await mountView();
    expect(wrapper.text()).toContain('交易日 16:30 触发');
    expect(wrapper.text()).toContain('数据同步已完成');
    expect(wrapper.text()).toContain('评估 2/2');
  });

  it('自动评估未开启：横幅提示启用方式', async () => {
    mockedGet.mockResolvedValue(makePayload({}));
    const { wrapper } = await mountView();
    expect(wrapper.text()).toContain('收盘自动评估未开启');
    expect(wrapper.text()).toContain('AP_AUTO_REVIEW=1');
  });

  it('stale 组合：暂无评估徽章 + 降级短句', async () => {
    mockedGet.mockResolvedValue(
      makePayload({
        portfolios: [
          makeItem({
            pid: 'hk1',
            name: '港股打新',
            run_uid: null,
            as_of: null,
            fresh: false,
            metrics: null,
            level: 'stale',
            highlights: ['暂无评估结论：等待收盘后自动评估，或到组合页手动触发评估'],
          }),
        ],
      }),
    );
    const { wrapper } = await mountView();
    expect(wrapper.text()).toContain('暂无评估');
    expect(wrapper.text()).toContain('尚无评估记录');
  });

  it('非当日结论：fresh=false 显示截至日期提示', async () => {
    mockedGet.mockResolvedValue(
      makePayload({
        date: '2026-10-27',
        portfolios: [makeItem({ as_of: '2026-10-26', fresh: false })],
      }),
    );
    const { wrapper } = await mountView();
    expect(wrapper.text()).toContain('截至 2026-10-26（收盘自动评估未跑当日，可手动触发评估）');
  });

  it('空组合列表展示占位文案', async () => {
    mockedGet.mockResolvedValue(makePayload({ portfolios: [] }));
    const { wrapper } = await mountView();
    expect(wrapper.text()).toContain('暂无组合');
  });

  it('手动刷新按钮重新拉取', async () => {
    mockedGet.mockResolvedValue(makePayload({}));
    const { wrapper } = await mountView();
    await wrapper.find('.ap-page__head .ck-link').trigger('click');
    await flushPromises();
    expect(mockedGet).toHaveBeenCalledTimes(2);
  });

  it('60s 自动刷新（fake timers）', async () => {
    vi.useFakeTimers();
    mockedGet.mockResolvedValue(makePayload({}));
    const { wrapper } = await mountView();
    expect(mockedGet).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60_000);
    await flushPromises();
    expect(mockedGet).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });

  it('卸载后定时器清理（无泄漏调用）', async () => {
    vi.useFakeTimers();
    mockedGet.mockResolvedValue(makePayload({}));
    const { wrapper } = await mountView();
    wrapper.unmount();
    const before = mockedGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(120_000);
    expect(mockedGet.mock.calls.length).toBe(before);
  });

  it('加载失败展示错误信息', async () => {
    mockedGet.mockRejectedValue(new Error('boom'));
    const { wrapper } = await mountView();
    expect(wrapper.text()).toContain('加载失败');
    expect(wrapper.text()).toContain('boom');
  });
});
