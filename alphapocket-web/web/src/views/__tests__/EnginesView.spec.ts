/**
 * 引擎板块（EnginesView）测试：
 * - 三引擎描述卡渲染 + 指标清单（表行/状态徽章）；
 * - 新增指标弹窗：提交成功 → toast + 刷新；data_missing → 提示条 + 引导链接；
 * - 停用（toggle）→ updateEngineMetric patch status；
 * - 软删弹窗：hypothesis_id 必填校验 → removeEngineMetric；
 * - 引擎数据集卡片：未同步条目 hint 展示。
 */
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import EnginesView from '../EnginesView.vue';

vi.mock('../../api', () => ({
  addEngineMetric: vi.fn(),
  describeEngines: vi.fn(),
  listEngineDatasets: vi.fn(),
  listEngineMetrics: vi.fn(),
  removeEngineMetric: vi.fn(),
  updateEngineMetric: vi.fn(),
}));

import {
  addEngineMetric,
  describeEngines,
  listEngineDatasets,
  listEngineMetrics,
  removeEngineMetric,
  updateEngineMetric,
} from '../../api';

const mAdd = vi.mocked(addEngineMetric);
const mDescribe = vi.mocked(describeEngines);
const mDatasets = vi.mocked(listEngineDatasets);
const mList = vi.mocked(listEngineMetrics);
const mRemove = vi.mocked(removeEngineMetric);
const mUpdate = vi.mocked(updateEngineMetric);

const DESCRIBES = {
  engines: [
    { id: 'alpha', name: '引擎A', kind: 'cross_section', metric_count: 15, active_count: 15 },
    { id: 'beta', name: '引擎B', kind: 'timing', metric_count: 6, active_count: 6 },
    { id: 'macro', name: '引擎C', kind: 'macro_regime', metric_count: 13, active_count: 13 },
  ],
};

const ALPHA_METRICS = {
  metrics: [
    {
      id: 'val_pe_ttm', engine: 'alpha', formula: 'builtin:pe_ttm_pit', dim: '估值',
      market: null, dataset: null, scorer: null, params: {}, status: 'active',
      version: 1, hypothesis_id: null, note: '',
    },
    {
      id: 'old_factor', engine: 'alpha', formula: 'builtin:margin', dim: '质量',
      market: null, dataset: null, scorer: null, params: {}, status: 'inactive',
      version: 3, hypothesis_id: 'H-0001', note: '已下线',
    },
  ],
  changelog_n: 5,
};

const DATASETS = {
  datasets: [
    {
      dataset: 'macro_liquidity', synced: false, as_of: null, series: 0,
      metrics: ['m2_yoy'], hint: '数据集 macro_liquidity 尚未同步，请前往数据页触发同步（incremental）',
    },
  ],
  missing: 1,
};

let router: Router;

function mountView() {
  // ApModal teleport 到 body：attachTo 真实 DOM 后经 DOMWrapper 查弹窗内容
  const div = document.createElement('div');
  document.body.appendChild(div);
  return mount(EnginesView, {
    attachTo: div,
    global: {
      plugins: [router],
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  });
}

/** 在 teleport 到 body 的弹窗内按 data-test 查元素。 */
function dq(sel: string): DOMWrapper<Element> {
  const el = document.querySelector(sel);
  if (!el) throw new Error(`body 内未找到 ${sel}`);
  return new DOMWrapper(el);
}

beforeEach(async () => {
  vi.clearAllMocks();
  mDescribe.mockResolvedValue(DESCRIBES);
  mList.mockResolvedValue(ALPHA_METRICS);
  mDatasets.mockResolvedValue(DATASETS);
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/engines', component: EnginesView },
      { path: '/data', component: { template: '<div />' } },
    ] as RouteRecordRaw[],
  });
  router.push('/engines');
  await router.isReady();
});

describe('EnginesView · 引擎板块', () => {
  it('渲染三引擎描述卡与指标清单', async () => {
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-test="ev-desc-alpha"]').exists()).toBe(true);
    expect(w.find('[data-test="ev-desc-macro"]').exists()).toBe(true);
    // 默认 alpha tab：两条指标行
    expect(w.find('[data-test="ev-table"]').text()).toContain('val_pe_ttm');
    expect(w.find('[data-test="ev-table"]').text()).toContain('old_factor');
    expect(w.text()).toContain('changelog');
  });

  it('新增指标成功 + 数据缺失提示条与引导链接', async () => {
    mAdd.mockResolvedValue({
      ok: true,
      metric: {
        id: 'm2_yoy_v2', engine: 'macro', formula: 'builtin:macro_series', dim: 'D1',
        market: 'A', dataset: 'macro_liquidity', scorer: { type: 'step', arg: [8, 10] },
        params: {}, status: 'active', version: 1, hypothesis_id: 'H-0002', note: '',
      },
      data_missing: [
        {
          dataset: 'macro_liquidity', synced: false, as_of: null, series: 0,
          hint: '数据集 macro_liquidity 尚未同步，请前往数据页触发同步（incremental）',
        },
      ],
    });
    const w = mountView();
    await flushPromises();

    await w.find('[data-test="ev-add"]').trigger('click');
    await dq('[data-test="ev-add-id"]').setValue('m2_yoy_v2');
    await dq('[data-test="ev-add-formula"]').setValue('builtin:macro_series');
    await dq('[data-test="ev-add-hypo"]').setValue('H-0002');
    await dq('[data-test="ev-add-submit"]').trigger('click');
    await flushPromises();

    expect(mAdd).toHaveBeenCalledOnce();
    // 数据缺失提示条 + 前往数据页链接（RouterLink stub 不透传 to，断文案）
    expect(w.find('[data-test="ev-missing-bar"]').text()).toContain('macro_liquidity');
    expect(w.find('[data-test="ev-missing-goto"]').text()).toContain('前往数据页');
  });

  it('停用指标 → updateEngineMetric(status=inactive)', async () => {
    mUpdate.mockResolvedValue({
      ok: true,
      before: ALPHA_METRICS.metrics[0],
      after: { ...ALPHA_METRICS.metrics[0], status: 'inactive', version: 2 },
    });
    const w = mountView();
    await flushPromises();

    await w.find('[data-test="ev-toggle-val_pe_ttm"]').trigger('click');
    await flushPromises();

    expect(mUpdate).toHaveBeenCalledWith('val_pe_ttm', {
      patch: { status: 'inactive' },
      hypothesis_id: undefined,
      reason: '停用',
    });
  });

  it('软删弹窗：hypothesis_id 缺失拦截 → 补齐后提交', async () => {
    mRemove.mockResolvedValue({
      ok: true,
      metric: { ...ALPHA_METRICS.metrics[0], status: 'inactive', version: 2 },
    });
    const w = mountView();
    await flushPromises();

    await w.find('[data-test="ev-remove-val_pe_ttm"]').trigger('click');
    expect(dq('[data-test="ev-remove-modal"]').exists()).toBe(true);

    // 未填 hypothesis_id → 拦截（不调 API）
    await dq('[data-test="ev-remove-submit"]').trigger('click');
    expect(mRemove).not.toHaveBeenCalled();

    await dq('[data-test="ev-remove-hypo"]').setValue('H-0001');
    await dq('[data-test="ev-remove-submit"]').trigger('click');
    await flushPromises();

    expect(mRemove).toHaveBeenCalledWith('val_pe_ttm', {
      hypothesis_id: 'H-0001',
      reason: '',
    });
  });

  it('引擎数据集卡片：未同步条目 + hint', async () => {
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-test="ev-datasets-missing"]').text()).toBe('1');
    expect(w.find('[data-test="ev-dataset-macro_liquidity"]').text()).toContain('未同步');
    expect(w.find('[data-test="ev-dataset-macro_liquidity"]').text()).toContain('m2_yoy');
  });
});
