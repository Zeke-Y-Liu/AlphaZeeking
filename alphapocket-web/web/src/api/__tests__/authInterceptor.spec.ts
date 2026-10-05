import { AxiosError, AxiosHeaders } from 'axios';
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiClient, configureAuth, resetAuthContext } from '../client';
import { ApiError } from '../types';
import type { AuthTokens } from '../types';

const TOKENS: AuthTokens = {
  access: 'acc-new',
  refresh: 'ref-new',
  token_type: 'bearer',
  expires_in: 900,
  user: { id: 1, username: 'demo', role: 'user' },
};

interface MockRoute {
  status: number;
  data?: unknown;
}

/**
 * 安装确定性 mock adapter（离线，不发真实网络请求）。
 * 模拟 axios 内置 adapter 的 settle：非 2xx 抛出带 response 的 AxiosError。
 */
function installAdapter(handler: (config: InternalAxiosRequestConfig) => MockRoute): void {
  apiClient.defaults.adapter = async (config) => {
    const cfg = config as InternalAxiosRequestConfig;
    const route = handler(cfg);
    const response = {
      data: route.data ?? {},
      status: route.status,
      statusText: String(route.status),
      headers: new AxiosHeaders(),
      config: cfg,
    } as AxiosResponse;
    const validate = cfg.validateStatus ?? ((s: number) => s >= 200 && s < 300);
    if (validate(response.status)) return response;
    throw new AxiosError(
      `Request failed with status code ${response.status}`,
      String(response.status),
      cfg,
      undefined,
      response,
    );
  };
}

beforeEach(() => {
  resetAuthContext();
  localStorage.clear();
});

afterEach(() => {
  resetAuthContext();
  apiClient.defaults.adapter = undefined;
});

describe('api client 401 刷新拦截', () => {
  it('401 → refresh → 重放成功', async () => {
    let refreshed = false;
    const refresh = vi.fn(async () => {
      refreshed = true;
      return TOKENS;
    });
    configureAuth({
      getAccess: () => (refreshed ? 'acc-new' : 'acc-old'),
      getRefresh: () => 'ref-old',
      refresh,
      onRefreshFailed: vi.fn(),
      redirectToLogin: vi.fn(),
    });
    installAdapter((cfg) =>
      cfg.url?.includes('/secured')
        ? refreshed
          ? { status: 200, data: { ok: true } }
          : { status: 401, data: { code: 'UNAUTHORIZED' } }
        : { status: 200, data: {} },
    );

    const res = await apiClient.get('/secured');

    expect(res.status).toBe(200);
    expect(res.data).toEqual({ ok: true });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('并发 3 个 401 只触发 1 次 refresh，且 3 个请求都重放成功', async () => {
    let refreshed = false;
    const refresh = vi.fn(async () => {
      refreshed = true;
      return TOKENS;
    });
    configureAuth({
      getAccess: () => 'acc-old',
      getRefresh: () => 'ref-old',
      refresh,
      onRefreshFailed: vi.fn(),
      redirectToLogin: vi.fn(),
    });
    installAdapter((cfg) =>
      cfg.url?.includes('/secured')
        ? refreshed
          ? { status: 200, data: { ok: true } }
          : { status: 401 }
        : { status: 200, data: {} },
    );

    const results = await Promise.all([
      apiClient.get('/secured'),
      apiClient.get('/secured'),
      apiClient.get('/secured'),
    ]);

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(results.map((r) => r.status)).toEqual([200, 200, 200]);
    expect(results.every((r) => r.data.ok === true)).toBe(true);
  });

  it('refresh 失败 → 清空凭证 + 跳转登录（副作用仅一次）', async () => {
    const onRefreshFailed = vi.fn();
    const redirectToLogin = vi.fn();
    const refresh = vi.fn(async () => {
      throw new ApiError('UNAUTHORIZED', 'refresh token 已失效', 401);
    });
    configureAuth({
      getAccess: () => 'acc-old',
      getRefresh: () => 'ref-old',
      refresh,
      onRefreshFailed,
      redirectToLogin,
    });
    installAdapter(() => ({ status: 401 }));

    await expect(apiClient.get('/secured')).rejects.toBeInstanceOf(ApiError);

    expect(onRefreshFailed).toHaveBeenCalledTimes(1);
    expect(redirectToLogin).toHaveBeenCalledTimes(1);
    expect(redirectToLogin.mock.calls[0][0]).toContain('/');
  });

  it('并发 401 刷新失败时副作用仍只执行一次', async () => {
    const onRefreshFailed = vi.fn();
    const redirectToLogin = vi.fn();
    configureAuth({
      getAccess: () => 'acc-old',
      getRefresh: () => 'ref-old',
      refresh: vi.fn(async () => {
        throw new ApiError('UNAUTHORIZED', 'refresh 失败', 401);
      }),
      onRefreshFailed,
      redirectToLogin,
    });
    installAdapter(() => ({ status: 401 }));

    await Promise.allSettled([apiClient.get('/a'), apiClient.get('/b'), apiClient.get('/c')]);

    expect(onRefreshFailed).toHaveBeenCalledTimes(1);
    expect(redirectToLogin).toHaveBeenCalledTimes(1);
  });

  it('/auth/login 的 401 不触发 refresh', async () => {
    const refresh = vi.fn();
    configureAuth({
      getAccess: () => null,
      getRefresh: () => null,
      refresh,
      onRefreshFailed: vi.fn(),
      redirectToLogin: vi.fn(),
    });
    installAdapter((cfg) =>
      cfg.url?.includes('/auth/login') ? { status: 401, data: { code: 'UNAUTHORIZED' } } : { status: 200 },
    );

    await expect(apiClient.post('/auth/login', { username: 'x', password: 'y' })).rejects.toBeInstanceOf(
      ApiError,
    );

    expect(refresh).toHaveBeenCalledTimes(0);
  });

  it('/auth/refresh 的 401 不触发 refresh（防死循环）', async () => {
    const refresh = vi.fn();
    configureAuth({
      getAccess: () => 'acc-old',
      getRefresh: () => 'ref-old',
      refresh,
      onRefreshFailed: vi.fn(),
      redirectToLogin: vi.fn(),
    });
    installAdapter(() => ({ status: 401 }));

    await expect(apiClient.post('/auth/refresh', { refresh: 'ref-old' })).rejects.toBeInstanceOf(ApiError);

    expect(refresh).toHaveBeenCalledTimes(0);
  });
});
