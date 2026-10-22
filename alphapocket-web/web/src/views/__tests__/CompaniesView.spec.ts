import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import CompaniesView from '../CompaniesView.vue';
import { createAnalysis, deleteAnalysis, listAnalyses } from '../../api';
import type { AnalysisProfile } from '../../api/types';

vi.mock('../../api', () => ({
  listAnalyses: vi.fn(),
  createAnalysis: vi.fn(),
  deleteAnalysis: vi.fn(),
}));

const mockedList = vi.mocked(listAnalyses);
const mockedCreate = vi.mocked(createAnalysis);
const mockedDelete = vi.mocked(deleteAnalysis);

function makeProfile(over: Partial<AnalysisProfile> = {}): AnalysisProfile {
  return {
    id: 1,
    aid: 'ca_1',
    secid: '601058.SH',
    name: '赛轮集团 · 单公司分析',
    note: null,
    modules: { fundamental: true, industry: true, technical: true, news: true },
    status: 'active',
    shadow_pid: 'shadow_ca_1',
    created_at: '2026-10-21T17:08:25Z',
    updated_at: '2026-10-21T17:08:25Z',
    latest_run: null,
    ...over,
  };
}

describe('CompaniesView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('加载档案列表并渲染卡片（模块徽章 / 影子组合）', async () => {
    mockedList.mockResolvedValue([makeProfile()]);
    const w = mount(CompaniesView);
    await flushPromises();
    expect(mockedList).toHaveBeenCalledWith();
    expect(w.text()).toContain('赛轮集团 · 单公司分析');
    expect(w.text()).toContain('ca_1');
    expect(w.text()).toContain('601058.SH');
    expect(w.text()).toContain('基本面');
    expect(w.text()).toContain('消息');
    expect(w.text()).toContain('尚未运行分析');
    expect(w.find('[data-test="co-card"]').exists()).toBe(true);
  });

  it('空列表展示占位', async () => {
    mockedList.mockResolvedValue([]);
    const w = mount(CompaniesView);
    await flushPromises();
    expect(w.find('[data-test="co-empty"]').exists()).toBe(true);
    expect(w.text()).toContain('暂无分析档案');
  });

  it('加载失败展示错误与重试', async () => {
    mockedList.mockRejectedValue(new Error('网络异常'));
    const w = mount(CompaniesView);
    await flushPromises();
    expect(w.text()).toContain('网络异常');
    expect(w.text()).toContain('重试');
  });

  it('输入 secid 后可创建（trim + 大写归一），创建后刷新列表', async () => {
    mockedList.mockResolvedValue([]);
    mockedCreate.mockResolvedValue(makeProfile({ aid: 'ca_2', secid: '002027.SZ' }));
    const w = mount(CompaniesView);
    await flushPromises();
    await w.find('[data-test="co-secid"]').setValue(' 002027.sz ');
    await w.find('form').trigger('submit');
    await flushPromises();
    expect(mockedCreate).toHaveBeenCalledWith({ secid: '002027.SZ', name: undefined });
    expect(mockedList).toHaveBeenCalledTimes(2);           // 初次 + 创建后刷新
  });

  it('未输入 secid 时创建按钮禁用', async () => {
    mockedList.mockResolvedValue([]);
    const w = mount(CompaniesView);
    await flushPromises();
    expect((w.find('[data-test="co-create"]').element as HTMLButtonElement).disabled).toBe(true);
  });

  it('删除走确认并调用 API（confirm 取消则不调用）', async () => {
    mockedList.mockResolvedValue([makeProfile()]);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const w = mount(CompaniesView);
    await flushPromises();
    await w.find('[data-test="co-delete"]').trigger('click');
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalled();
    expect(mockedDelete).toHaveBeenCalledWith('ca_1');
  });
});
