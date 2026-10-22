import { describe, expect, it } from 'vitest';

import { router } from '../index';

describe('router 路由表（§8）', () => {
  const paths = router.getRoutes().map((r) => r.path);

  it('保留 /login 与 /portfolios', () => {
    expect(paths).toContain('/login');
    expect(paths).toContain('/portfolios');
  });

  it('包含组合相关子路由', () => {
    expect(paths).toContain('/portfolios/new');
    expect(paths).toContain('/portfolios/:id');
    expect(paths).toContain('/portfolios/:id/backtest');
    expect(paths).toContain('/portfolios/:id/rebalance');
  });

  it('包含数据 / 运行 / 我的 与 404 捕获', () => {
    expect(paths).toContain('/data');
    expect(paths).toContain('/runs');
    expect(paths).toContain('/me');
    expect(paths).toContain('/:pathMatch(.*)*');
  });

  it('包含单公司分析路由（M10）', () => {
    expect(paths).toContain('/companies');
    expect(paths).toContain('/companies/:aid');
    expect(router.resolve('/companies/ca_1').name).toBe('company-detail');
  });

  it('根路径重定向到 /portfolios', () => {
    expect(router.resolve('/').redirectedFrom).toBeUndefined();
    expect(router.resolve('/')).toBeDefined();
    const root = router.getRoutes().find((r) => r.path === '/');
    expect(root?.redirect).toBe('/portfolios');
  });

  it('各业务路由带 meta.title（用于 document.title）', () => {
    const named = router.getRoutes().filter((r) => typeof r.meta.title === 'string');
    expect(named.length).toBeGreaterThanOrEqual(8);
  });

  it('/portfolios/new 不被 /portfolios/:id 抢先匹配', () => {
    expect(router.resolve('/portfolios/new').name).toBe('portfolio-new');
    expect(router.resolve('/portfolios/multi5').name).toBe('portfolio-detail');
  });
});
