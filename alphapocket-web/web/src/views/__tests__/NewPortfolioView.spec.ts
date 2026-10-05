import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { RouteRecordRaw, Router } from 'vue-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/types';
import type { AssetType, Portfolio, Security } from '../../api/types';
import NewPortfolioView from '../NewPortfolioView.vue';

vi.mock('../../api', () => ({
  createPortfolio: vi.fn(),
  registerSecurity: vi.fn(),
  searchSecurities: vi.fn(),
}));

import { createPortfolio, registerSecurity, searchSecurities } from '../../api';

const mockedCreate = vi.mocked(createPortfolio);
const mockedRegister = vi.mocked(registerSecurity);
const mockedSearch = vi.mocked(searchSecurities);

const Stub = { template: '<div />' };

async function mountWizard(): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const routes: RouteRecordRaw[] = [
    { path: '/portfolios/new', name: 'portfolio-new', component: Stub },
    { path: '/portfolios/:id', name: 'portfolio-detail', component: Stub },
    { path: '/portfolios', name: 'portfolios', component: Stub },
    { path: '/data', name: 'data', component: Stub },
  ];
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push('/portfolios/new');
  await router.isReady();
  const wrapper = mount(NewPortfolioView, { global: { plugins: [router] } });
  return { wrapper, router };
}

function makeSecurity(code: string, assetType: AssetType = 'STOCK'): Security {
  return {
    secid: `${code}.SH`,
    code,
    name: `标的${code}`,
    market: 'A',
    asset_type: assetType,
    sina_symbol: `sh${code}`,
    source: 'user',
    created_at: '2024-01-01T00:00:00Z',
  };
}

function makePortfolio(pid: string, name = '多资产测试组合'): Portfolio {
  return {
    id: 1,
    pid,
    name,
    base_ccy: 'CNY',
    strategy: {},
    status: 'active',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    position_count: 1,
  };
}

async function toPositionsStep(wrapper: ReturnType<typeof mount>): Promise<void> {
  await wrapper.find('[data-test="name"]').setValue('多资产测试组合');
  await wrapper.find('[data-test="to-positions"]').trigger('click');
  await flushPromises();
}

async function addViaRegister(
  wrapper: ReturnType<typeof mount>,
  code = '510300',
  type: AssetType = 'STOCK',
): Promise<void> {
  await wrapper.find('[data-test="register-code"]').setValue(code);
  await wrapper.find('[data-test="register-type"]').setValue(type);
  await wrapper.find('[data-test="register-btn"]').trigger('click');
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedSearch.mockResolvedValue([]);
  mockedRegister.mockImplementation(async (code, assetType) =>
    makeSecurity(code, assetType ?? 'STOCK'),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

describe('NewPortfolioView 三步向导', () => {
  it('三步切换：基本信息 → 标的与权重 → 确认', async () => {
    const { wrapper } = await mountWizard();
    expect(wrapper.find('[data-test="step-basic"]').exists()).toBe(true);

    await toPositionsStep(wrapper);
    expect(wrapper.find('[data-test="step-positions"]').exists()).toBe(true);

    await addViaRegister(wrapper);
    await wrapper.find('[data-test="weight-input"]').setValue('100');
    await wrapper.find('[data-test="to-confirm"]').trigger('click');
    expect(wrapper.find('[data-test="step-confirm"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="summary-name"]').text()).toBe('多资产测试组合');
  });

  it('名称为空时无法进入下一步', async () => {
    const { wrapper } = await mountWizard();
    await wrapper.find('[data-test="to-positions"]').trigger('click');
    expect(wrapper.find('[data-test="step-basic"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="name-error"]').text()).toContain('名称');
  });

  it('搜索下拉防抖调用 searchSecurities（延迟前不触发，后触发一次）', async () => {
    vi.useFakeTimers();
    const { wrapper } = await mountWizard();
    await toPositionsStep(wrapper);

    await wrapper.find('[data-test="search-input"]').setValue('51');
    expect(mockedSearch).not.toHaveBeenCalled();

    vi.advanceTimersByTime(299);
    await flushPromises();
    expect(mockedSearch).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    await flushPromises();
    expect(mockedSearch).toHaveBeenCalledTimes(1);
    expect(mockedSearch).toHaveBeenCalledWith('51');
  });

  it('新标的即时 register（带 asset_type）并加入持仓', async () => {
    const { wrapper } = await mountWizard();
    await toPositionsStep(wrapper);

    await addViaRegister(wrapper, '159915', 'ETF');

    expect(mockedRegister).toHaveBeenCalledWith('159915', 'ETF');
    expect(wrapper.findAll('[data-test="weight-input"]')).toHaveLength(1);
    expect(wrapper.text()).toContain('159915.SH');
  });

  it('等权一键 → 5 个 20.0，合计 100.0%', async () => {
    vi.useFakeTimers();
    mockedSearch.mockResolvedValue(
      Array.from({ length: 5 }, (_, i) => makeSecurity(`51030${i}`, 'ETF')),
    );
    const { wrapper } = await mountWizard();
    await toPositionsStep(wrapper);

    await wrapper.find('[data-test="search-input"]').setValue('510');
    vi.advanceTimersByTime(300);
    await flushPromises();

    const results = wrapper.findAll('[data-test="search-result"]');
    expect(results).toHaveLength(5);
    for (const r of results) await r.trigger('click');

    await wrapper.find('[data-test="equal-weight"]').trigger('click');

    const inputs = wrapper.findAll('[data-test="weight-input"]');
    expect(inputs).toHaveLength(5);
    inputs.forEach((input) => {
      expect((input.element as HTMLInputElement).value).toBe('20');
    });
    expect(wrapper.find('[data-test="weight-sum"]').text()).toContain('100.0');
  });

  it('手输 99/101 越界 → 出现归一提示且阻断进入确认', async () => {
    const { wrapper } = await mountWizard();
    await toPositionsStep(wrapper);
    await addViaRegister(wrapper);

    const weight = wrapper.find('[data-test="weight-input"]');
    await weight.setValue('99');
    expect(wrapper.find('[data-test="normalize-hint"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="normalize-hint"]').text()).toContain('99.0');
    expect(wrapper.find('[data-test="normalize-hint"]').text()).toContain('100.00');

    const next = wrapper.find('[data-test="to-confirm"]');
    expect(next.attributes('disabled')).toBeDefined();
    await next.trigger('click');
    expect(wrapper.find('[data-test="step-confirm"]').exists()).toBe(false);

    await weight.setValue('101');
    expect(wrapper.find('[data-test="normalize-hint"]').exists()).toBe(true);
    expect(next.attributes('disabled')).toBeDefined();
  });

  it('提交成功后跳转组合详情', async () => {
    mockedCreate.mockResolvedValue(makePortfolio('multi5'));
    const { wrapper, router } = await mountWizard();
    await toPositionsStep(wrapper);
    await addViaRegister(wrapper);
    await wrapper.find('[data-test="weight-input"]').setValue('100');
    await wrapper.find('[data-test="to-confirm"]').trigger('click');
    await wrapper.find('[data-test="submit"]').trigger('click');
    await flushPromises();

    expect(mockedCreate).toHaveBeenCalledTimes(1);
    expect(mockedCreate.mock.calls[0][0]).toMatchObject({
      name: '多资产测试组合',
      positions: [{ secid: '510300.SH', weight: 100 }],
    });
    expect(router.currentRoute.value.path).toBe('/portfolios/multi5');
  });

  it('勾选「立即同步数据」→ 提交后跳转 /data', async () => {
    mockedCreate.mockResolvedValue(makePortfolio('multi5'));
    const { wrapper, router } = await mountWizard();
    await toPositionsStep(wrapper);
    await addViaRegister(wrapper);
    await wrapper.find('[data-test="weight-input"]').setValue('100');
    await wrapper.find('[data-test="to-confirm"]').trigger('click');
    await wrapper.find('[data-test="sync-now"]').setValue(true);
    await wrapper.find('[data-test="submit"]').trigger('click');
    await flushPromises();

    expect(router.currentRoute.value.path).toBe('/data');
  });

  it('422 WEIGHT_NOT_NORMALIZED → 回退权重步内联展示（含 suggestion）', async () => {
    mockedCreate.mockRejectedValue(
      new ApiError('WEIGHT_NOT_NORMALIZED', '权重和未归一', 422, {
        suggestion: [{ secid: '510300.SH', weight: 100.0 }],
      }),
    );
    const { wrapper } = await mountWizard();
    await toPositionsStep(wrapper);
    await addViaRegister(wrapper);
    await wrapper.find('[data-test="weight-input"]').setValue('100');
    await wrapper.find('[data-test="to-confirm"]').trigger('click');
    await wrapper.find('[data-test="submit"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="step-positions"]').exists()).toBe(true);
    const err = wrapper.find('[data-test="weights-error"]');
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain('权重和未归一');
    expect(err.text()).toContain('510300.SH 100.00');
  });

  it('422 SECURITY_NOT_FOUND → 内联到标的错误', async () => {
    mockedCreate.mockRejectedValue(
      new ApiError('SECURITY_NOT_FOUND', '标的不可达或代码无效：510300', 422, { code: '510300' }),
    );
    const { wrapper } = await mountWizard();
    await toPositionsStep(wrapper);
    await addViaRegister(wrapper);
    await wrapper.find('[data-test="weight-input"]').setValue('100');
    await wrapper.find('[data-test="to-confirm"]').trigger('click');
    await wrapper.find('[data-test="submit"]').trigger('click');
    await flushPromises();

    const err = wrapper.find('[data-test="positions-error"]');
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain('510300');
  });

  it('422 VALIDATION_ERROR（name）→ 内联到名称字段', async () => {
    mockedCreate.mockRejectedValue(new ApiError('VALIDATION_ERROR', 'name 不能为空', 422));
    const { wrapper } = await mountWizard();
    await toPositionsStep(wrapper);
    await addViaRegister(wrapper);
    await wrapper.find('[data-test="weight-input"]').setValue('100');
    await wrapper.find('[data-test="to-confirm"]').trigger('click');
    await wrapper.find('[data-test="submit"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="step-basic"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="name-error"]').text()).toContain('name 不能为空');
  });
});
