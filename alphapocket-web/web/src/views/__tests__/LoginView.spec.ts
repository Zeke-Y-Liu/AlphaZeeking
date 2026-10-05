import { flushPromises, mount } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { Router, RouteRecordRaw } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/types';
import type { AuthTokens } from '../../api/types';
import LoginView from '../LoginView.vue';

vi.mock('../../api', () => ({
  authLogin: vi.fn(),
  authRefresh: vi.fn(),
}));

import { authLogin } from '../../api';

const mockedLogin = vi.mocked(authLogin);

const TOKENS: AuthTokens = {
  access: 'acc-1',
  refresh: 'ref-1',
  token_type: 'bearer',
  expires_in: 900,
  user: { id: 1, username: 'demo', role: 'user' },
};

const Stub = { template: '<div />' };

async function mountLogin(query = ''): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const routes: RouteRecordRaw[] = [
    { path: '/login', name: 'login', component: Stub },
    { path: '/portfolios', name: 'portfolios', component: Stub },
    { path: '/data', name: 'data', component: Stub },
  ];
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push(`/login${query}`);
  await router.isReady();
  const wrapper = mount(LoginView, { global: { plugins: [createPinia(), router] } });
  return { wrapper, router };
}

async function submit(wrapper: ReturnType<typeof mount>, username = 'demo', password = 'bad'): Promise<void> {
  await wrapper.find('[data-test="username"]').setValue(username);
  await wrapper.find('[data-test="password"]').setValue(password);
  await wrapper.find('form').trigger('submit');
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

describe('LoginView', () => {
  it('401 显示内联「用户名或密码错误」', async () => {
    mockedLogin.mockRejectedValue(new ApiError('UNAUTHORIZED', '用户名或密码错误', 401));
    const { wrapper } = await mountLogin();

    await submit(wrapper);

    expect(wrapper.find('[data-test="error"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="error"]').text()).toContain('用户名或密码错误');
    expect(wrapper.find('[data-test="locked"]').exists()).toBe(false);
  });

  it('429 显示账号锁定剩余时间提示', async () => {
    mockedLogin.mockRejectedValue(new ApiError('ACCOUNT_LOCKED', '账号已锁定，请 15 分钟后再试', 429));
    const { wrapper } = await mountLogin();

    await submit(wrapper);

    expect(wrapper.find('[data-test="locked"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="locked"]').text()).toContain('15');
    expect(wrapper.find('[data-test="error"]').exists()).toBe(false);
  });

  it('提交中按钮禁用', async () => {
    let resolveLogin: (tokens: AuthTokens) => void = () => {};
    mockedLogin.mockImplementation(
      () =>
        new Promise<AuthTokens>((resolve) => {
          resolveLogin = resolve;
        }),
    );
    const { wrapper } = await mountLogin();

    await submit(wrapper, 'demo', 'alphapocket');

    const button = wrapper.find('[data-test="submit"]');
    expect(button.attributes('disabled')).toBeDefined();
    expect(button.classes()).toContain('is-loading');

    resolveLogin(TOKENS);
    await flushPromises();
  });

  it('登录成功跳转 redirect 目标', async () => {
    mockedLogin.mockResolvedValue(TOKENS);
    const { wrapper, router } = await mountLogin('?redirect=/data');

    await submit(wrapper, 'demo', 'alphapocket');

    expect(router.currentRoute.value.path).toBe('/data');
    expect(localStorage.getItem('ap.access')).toBe('acc-1');
  });

  it('无 redirect 参数时跳转 /portfolios', async () => {
    mockedLogin.mockResolvedValue(TOKENS);
    const { wrapper, router } = await mountLogin();

    await submit(wrapper, 'demo', 'alphapocket');

    expect(router.currentRoute.value.path).toBe('/portfolios');
  });
});
