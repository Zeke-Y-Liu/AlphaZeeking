import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import RunsView from '../RunsView.vue';
import { fetchRunArtifact, listRuns } from '../../api';
import type { Run } from '../../api/types';

vi.mock('../../api', () => ({
  listRuns: vi.fn(),
  fetchRunArtifact: vi.fn(),
  runArtifactUrl: vi.fn(() => '/api/v1/runs/x/artifacts/review.json'),
}));

const mockedRuns = vi.mocked(listRuns);
const mockedArtifact = vi.mocked(fetchRunArtifact);

function makeRun(over: Partial<Run>): Run {
  return {
    id: 1,
    run_uid: 'run_20261018_000000_ab12cd',
    job_id: 'job_20261018_000000_ab12cd',
    portfolio_id: 1,
    type: 'review',
    as_of: '2026-10-16',
    summary: {},
    artifact_keys: ['review.json'],
    created_at: '2026-10-18T01:00:00',
    ...over,
  } as Run;
}

describe('RunsView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('加载 runs 并渲染表格行（类型徽章 / run_uid / 产物按钮）', async () => {
    mockedRuns.mockResolvedValue([
      makeRun({}),
      makeRun({ run_uid: 'run_bt_01', type: 'backtest', artifact_keys: ['report.html'] }),
    ]);
    const w = mount(RunsView);
    await flushPromises();
    expect(mockedRuns).toHaveBeenCalledWith({ type: undefined, limit: 100 });
    expect(w.findAll('tbody tr')).toHaveLength(2);
    expect(w.text()).toContain('评估');
    expect(w.text()).toContain('回测');
    expect(w.text()).toContain('预览');
  });

  it('type 筛选 chips 触发带 type 的重新加载', async () => {
    mockedRuns.mockResolvedValue([]);
    const w = mount(RunsView);
    await flushPromises();
    await w.findAll('.ck-chip')[1].trigger('click');
    await flushPromises();
    expect(mockedRuns).toHaveBeenLastCalledWith({ type: 'review', limit: 100 });
  });

  it('预览弹窗：json 产物格式化展示', async () => {
    mockedRuns.mockResolvedValue([makeRun({})]);
    mockedArtifact.mockResolvedValue({ as_of: '2026-10-16', positions: [1, 2] });
    const w = mount(RunsView);
    await flushPromises();
    await w.findAll('.ck-link')[0].trigger('click');
    await flushPromises();
    expect(mockedArtifact).toHaveBeenCalledWith('run_20261018_000000_ab12cd', 'review.json', 'json');
    expect(w.find('.ck-pre').text()).toContain('"as_of": "2026-10-16"');
  });

  it('空列表展示占位文案', async () => {
    mockedRuns.mockResolvedValue([]);
    const w = mount(RunsView);
    await flushPromises();
    expect(w.text()).toContain('暂无运行记录');
  });
});
