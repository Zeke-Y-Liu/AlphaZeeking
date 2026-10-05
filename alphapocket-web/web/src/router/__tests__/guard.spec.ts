import { createPinia, setActivePinia } from 'pinia';
import { createMemoryHistory } from 'vue-router';
import { beforeEach, describe, expect, it } from 'vitest';

import { useAuthStore } from '../../stores/auth';
import { createAppRouter } from '../index';

function loginAsDemo(): void {
  useAuthStore().applyTokens({
    access: 'acc-1',
    refresh: 'ref-1',
    token_type: 'bearer',
    expires_in: 900,
    user: { id: 1, username: 'demo', role: 'user' },
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

describe('认证路由守卫', () => {
  it('未认证访问受保护路由 → 重定向 /login 且带 redirect', async () => {
    const router = createAppRouter(createMemoryHistory());

    await router.push('/portfolios');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query.redirect).toBe('/portfolios');
  });

  it('未认证访问 .fullPath 带 query 时 redirect 保留完整路径', async () => {
    const router = createAppRouter(createMemoryHistory());

    await router.push('/portfolios/new?step=2');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query.redirect).toBe('/portfolios/new?step=2');
  });

  it('已认证访问 /login → 重定向 /portfolios', async () => {
    loginAsDemo();
    const router = createAppRouter(createMemoryHistory());

    await router.push('/login');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/portfolios');
  });

  it('已认证访问 /login?redirect=/data → 重定向 redirect 目标', async () => {
    loginAsDemo();
    const router = createAppRouter(createMemoryHistory());

    await router.push('/login?redirect=/data');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/data');
  });

  it('未认证访问 /login 放行', async () => {
    const router = createAppRouter(createMemoryHistory());

    await router.push('/login');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/login');
  });

  it('已认证访问受保护路由放行', async () => {
    loginAsDemo();
    const router = createAppRouter(createMemoryHistory());

    await router.push('/data');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/data');
  });

  it('/login 路由标记 layout: blank 与 requiresAuth: false', () => {
    const router = createAppRouter(createMemoryHistory());
    const login = router.getRoutes().find((r) => r.path === '/login');
    expect(login?.meta.layout).toBe('blank');
    expect(login?.meta.requiresAuth).toBe(false);
  });
});
