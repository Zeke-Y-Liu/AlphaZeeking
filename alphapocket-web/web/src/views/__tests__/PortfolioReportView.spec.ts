import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Portfolio, Run } from '../../api/types';
import PortfolioReportView from '../PortfolioReportView.vue';

vi.mock('../../api', () => ({
  fetchRunArtifact: vi.fn(),
  getPortfolio: vi.fn(),
  listRuns: vi.fn(),
}));

import { fetchRunArtifact, getPortfolio, listRuns } from '../../api';

const mGetPortfolio = vi.mocked(getPortfolio);
const mListRuns = vi.mocked(listRuns);
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
    artifact_keys: ['report.html'],
    created_at: '2024-06-28T00:00:00Z',
  };
}

async function mountReport(
  path = '/portfolios/1/report',
): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const routes: RouteRecordRaw[] = [
    { path: '/portfolios', name: 'portfolios', component: Stub },
    { path: '/portfolios/:id', name: 'portfolio-detail', component: Stub },
    { path: '/portfolios/:id/report', name: 'portfolio-report', component: Stub },
  ];
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(PortfolioReportView, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('PortfolioReportView', () => {
  it('鉴权取 report.html 绑定 iframe srcdoc，并提供 md/json 下载链接', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([makeRun()]);
    mArtifact.mockResolvedValue('<html><body>组合评估报告</body></html>');

    const { wrapper } = await mountReport();

    const frame = wrapper.find('[data-test="report-frame"]');
    expect(frame.exists()).toBe(true);
    expect(frame.attributes('srcdoc')).toContain('组合评估报告');
    expect(wrapper.find('[data-test="dl-md"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="dl-json"]').exists()).toBe(true);

    expect(mArtifact).toHaveBeenCalledWith('run-x', 'report.html', 'text');
  });

  it('无 review run → 空态提示', async () => {
    mGetPortfolio.mockResolvedValue(makePortfolio());
    mListRuns.mockResolvedValue([]);

    const { wrapper } = await mountReport();

    expect(wrapper.find('[data-test="rp-empty"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="report-frame"]').exists()).toBe(false);
  });
});
