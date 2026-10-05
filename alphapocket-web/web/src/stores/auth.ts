import { defineStore } from 'pinia';

import { authLogin, authRefresh } from '../api';
import { ApiError } from '../api/types';
import type { AuthTokens, AuthUser } from '../api/types';

/** localStorage 键（token 走响应体，此处持久化）。 */
export const ACCESS_KEY = 'ap.access';
export const REFRESH_KEY = 'ap.refresh';
export const USER_KEY = 'ap.user';

/*
 * ⚠️ XSS 取舍（T6.2 明确允许的降级方案）：
 * BFF 当前未提供 httpOnly Cookie，token 暂存 localStorage —— 任何同源 XSS 均可读取凭证，
 * 这是已知的安全降级。为便于日后切换为 httpOnly Cookie（+ CSRF 双提交），
 * 本 store 是**唯一**读写 localStorage 的位置；token 不写入日志、不落 URL。
 * 严禁在此存储密码。
 */

function readStoredUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as AuthUser;
    if (parsed && typeof parsed.id === 'number' && typeof parsed.username === 'string') return parsed;
    return null;
  } catch {
    return null;
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    access: null as string | null,
    refresh: null as string | null,
    user: null as AuthUser | null,
    loading: false,
    error: null as ApiError | null,
    /** restore() 是否已执行（避免每次导航重复读取 localStorage）。 */
    hydrated: false,
  }),
  getters: {
    isAuthenticated: (state) => !!state.access && !!state.user,
  },
  actions: {
    /** 登录：成功后写 state + localStorage；失败抛出 ApiError（页面据 code/status 展示，429 为锁定）。 */
    async login(username: string, password: string): Promise<void> {
      this.loading = true;
      this.error = null;
      try {
        const tokens = await authLogin({ username, password });
        this.applyTokens(tokens);
      } catch (err) {
        this.error = err instanceof ApiError ? err : new ApiError('UNKNOWN', String(err));
        throw this.error;
      } finally {
        this.loading = false;
      }
    },

    /** 刷新 token：成功更新 state + localStorage 并返回新凭证；失败抛出（由调用方清理）。 */
    async refreshTokens(): Promise<AuthTokens> {
      if (!this.refresh) {
        throw new ApiError('UNAUTHORIZED', '缺少 refresh token', 401);
      }
      const tokens = await authRefresh({ refresh: this.refresh });
      this.applyTokens(tokens);
      return tokens;
    },

    /** 应用启动时从 localStorage 恢复；幂等（hydrated 标记）。凭证不完整则清空。 */
    restore(): void {
      if (this.hydrated) return;
      const access = localStorage.getItem(ACCESS_KEY);
      const refresh = localStorage.getItem(REFRESH_KEY);
      const user = readStoredUser();
      if (access && refresh && user) {
        this.access = access;
        this.refresh = refresh;
        this.user = user;
      } else {
        this.clear();
      }
      this.hydrated = true;
    },

    /** 写入凭证（登录/刷新成功后调用），同步 localStorage。 */
    applyTokens(tokens: AuthTokens): void {
      this.access = tokens.access;
      this.refresh = tokens.refresh;
      this.user = tokens.user;
      localStorage.setItem(ACCESS_KEY, tokens.access);
      localStorage.setItem(REFRESH_KEY, tokens.refresh);
      localStorage.setItem(USER_KEY, JSON.stringify(tokens.user));
    },

    /** 登出：清空 state + localStorage。 */
    logout(): void {
      this.clear();
    },

    /** 清空凭证（state + localStorage）；跳转由调用方（router）负责。 */
    clear(): void {
      this.access = null;
      this.refresh = null;
      this.user = null;
      this.error = null;
      localStorage.removeItem(ACCESS_KEY);
      localStorage.removeItem(REFRESH_KEY);
      localStorage.removeItem(USER_KEY);
    },
  },
});
