import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/types';
import type { AuthTokens } from '../../api/types';
import { ACCESS_KEY, REFRESH_KEY, USER_KEY, useAuthStore } from '../auth';

vi.mock('../../api', () => ({
  authLogin: vi.fn(),
  authRefresh: vi.fn(),
}));

import { authLogin, authRefresh } from '../../api';

const mockedLogin = vi.mocked(authLogin);
const mockedRefresh = vi.mocked(authRefresh);

const TOKENS: AuthTokens = {
  access: 'acc-1',
  refresh: 'ref-1',
  token_type: 'bearer',
  expires_in: 900,
  user: { id: 1, username: 'z', role: 'user' },
};

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
  vi.clearAllMocks();
});

describe('auth store', () => {
  it('login 成功写 state + localStorage', async () => {
    mockedLogin.mockResolvedValue(TOKENS);
    const auth = useAuthStore();

    await auth.login('z', 'Alpha618');

    expect(auth.access).toBe('acc-1');
    expect(auth.refresh).toBe('ref-1');
    expect(auth.user?.username).toBe('z');
    expect(auth.isAuthenticated).toBe(true);
    expect(auth.loading).toBe(false);
    expect(localStorage.getItem(ACCESS_KEY)).toBe('acc-1');
    expect(localStorage.getItem(REFRESH_KEY)).toBe('ref-1');
    expect(JSON.parse(localStorage.getItem(USER_KEY) as string)).toEqual(TOKENS.user);
    expect(mockedLogin).toHaveBeenCalledWith({ username: 'z', password: 'Alpha618' });
  });

  it('logout 清空 state + localStorage', async () => {
    mockedLogin.mockResolvedValue(TOKENS);
    const auth = useAuthStore();
    await auth.login('z', 'Alpha618');

    auth.logout();

    expect(auth.access).toBeNull();
    expect(auth.refresh).toBeNull();
    expect(auth.user).toBeNull();
    expect(auth.isAuthenticated).toBe(false);
    expect(localStorage.getItem(ACCESS_KEY)).toBeNull();
    expect(localStorage.getItem(REFRESH_KEY)).toBeNull();
    expect(localStorage.getItem(USER_KEY)).toBeNull();
  });

  it('restore 从 localStorage 恢复', () => {
    localStorage.setItem(ACCESS_KEY, 'acc-9');
    localStorage.setItem(REFRESH_KEY, 'ref-9');
    localStorage.setItem(USER_KEY, JSON.stringify({ id: 2, username: 'restored', role: 'admin' }));

    const auth = useAuthStore();
    auth.restore();

    expect(auth.access).toBe('acc-9');
    expect(auth.refresh).toBe('ref-9');
    expect(auth.user?.username).toBe('restored');
    expect(auth.isAuthenticated).toBe(true);
  });

  it('restore 凭证不完整时清空', () => {
    localStorage.setItem(ACCESS_KEY, 'acc-only');

    const auth = useAuthStore();
    auth.restore();

    expect(auth.access).toBeNull();
    expect(auth.isAuthenticated).toBe(false);
    expect(localStorage.getItem(ACCESS_KEY)).toBeNull();
  });

  it('refreshTokens 更新 state 与 localStorage', async () => {
    localStorage.setItem(ACCESS_KEY, 'acc-old');
    localStorage.setItem(REFRESH_KEY, 'ref-old');
    localStorage.setItem(USER_KEY, JSON.stringify(TOKENS.user));
    mockedRefresh.mockResolvedValue({ ...TOKENS, access: 'acc-new', refresh: 'ref-new' });

    const auth = useAuthStore();
    auth.restore();
    const tokens = await auth.refreshTokens();

    expect(tokens.access).toBe('acc-new');
    expect(auth.access).toBe('acc-new');
    expect(localStorage.getItem(ACCESS_KEY)).toBe('acc-new');
    expect(mockedRefresh).toHaveBeenCalledWith({ refresh: 'ref-old' });
  });

  it('429 错误通过 error 暴露给页面', async () => {
    mockedLogin.mockRejectedValue(new ApiError('ACCOUNT_LOCKED', '账号已锁定，请 15 分钟后再试', 429));
    const auth = useAuthStore();

    await expect(auth.login('z', 'bad')).rejects.toBeInstanceOf(ApiError);

    expect(auth.loading).toBe(false);
    expect(auth.error?.status).toBe(429);
    expect(auth.error?.code).toBe('ACCOUNT_LOCKED');
    expect(auth.error?.message).toContain('15');
    expect(auth.isAuthenticated).toBe(false);
  });
});
