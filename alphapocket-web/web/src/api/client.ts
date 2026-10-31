import axios, { AxiosError } from 'axios';
import type { InternalAxiosRequestConfig } from 'axios';

import { ApiError, type ApiErrorBody, type AuthTokens } from './types';

/** 生成请求追踪 ID（优先 crypto.randomUUID，降级时间戳+随机）。 */
export function newRequestId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** HTTP 状态 → 兜底错误码（后端未返回 {code} 时使用）。 */
const STATUS_CODE: Record<number, string> = {
  400: 'BAD_REQUEST',
  401: 'UNAUTHORIZED',
  403: 'FORBIDDEN',
  404: 'NOT_FOUND',
  409: 'CONFLICT',
  422: 'UNPROCESSABLE_ENTITY',
  429: 'RATE_LIMITED',
  500: 'INTERNAL_ERROR',
  502: 'BAD_GATEWAY',
  503: 'SERVICE_UNAVAILABLE',
  504: 'GATEWAY_TIMEOUT',
};

function normalizeBody(data: unknown): Partial<ApiErrorBody> {
  if (!data || typeof data !== 'object') return {};
  const body = data as Record<string, unknown>;
  return {
    code: typeof body.code === 'string' ? body.code : undefined,
    message: typeof body.message === 'string' ? body.message : undefined,
    detail: body.detail,
  };
}

/** 把 axios 错误统一解包为 ApiError（网络/502 等也有明确 code）。 */
export function toApiError(error: AxiosError): ApiError {
  const status = error.response?.status ?? 0;
  if (status > 0) {
    const body = normalizeBody(error.response?.data);
    return new ApiError(
      body.code ?? STATUS_CODE[status] ?? `HTTP_${status}`,
      body.message ?? `请求失败（HTTP ${status}）`,
      status,
      body.detail,
    );
  }
  const code = error.code === 'ECONNABORTED' ? 'TIMEOUT' : 'NETWORK_ERROR';
  return new ApiError(code, error.message || '网络异常，请稍后重试', 0, undefined);
}

/* ── 认证上下文（由应用装配层注入，避免 client ↔ store/router 循环依赖） ── */

/**
 * 认证上下文：仅需 5 个轻量钩子。token 的读取、刷新落库、清理与跳转均交由注入方
 * （见 main.ts 装配、store 的 refreshTokens/clear），client.ts 不直接 import store。
 */
export interface AuthContext {
  /** 当前 access token（未登录返回 null）。 */
  getAccess: () => string | null;
  /** 当前 refresh token（未登录返回 null）。 */
  getRefresh: () => string | null;
  /** 执行一次刷新并落库（store.refreshTokens），成功返回新 token 三件套。 */
  refresh: () => Promise<AuthTokens>;
  /** 刷新失败时清空凭证（store.clear）。 */
  onRefreshFailed: () => void;
  /** 刷新失败时跳转登录页（携带 redirect 路径）。 */
  redirectToLogin: (redirectPath: string) => void;
}

let authContext: AuthContext | null = null;

/** 注入认证上下文（main.ts 启动时调用一次；测试可注入 mock）。 */
export function configureAuth(context: AuthContext): void {
  authContext = context;
}

/** 清除已注入的认证上下文（测试隔离用）。 */
export function resetAuthContext(): void {
  authContext = null;
  refreshPromise = null;
}

/** 无需触发 401 刷新逻辑的端点（防死循环）。 */
const AUTH_ENDPOINTS = ['/auth/login', '/auth/refresh'];

function isAuthEndpoint(url?: string): boolean {
  if (!url) return false;
  return AUTH_ENDPOINTS.some((path) => url.includes(path));
}

function currentPath(): string {
  if (typeof window === 'undefined') return '/';
  return `${window.location.pathname}${window.location.search}`;
}

/* ── 单飞刷新：并发 401 共用同一 Promise，只向 /auth/refresh 发一次请求 ── */

let refreshPromise: Promise<AuthTokens> | null = null;

/**
 * 单飞刷新 access token。任意时刻多个 401 只会触发一次刷新，其余调用等待同一次结果；
 * 刷新失败时统一清空凭证并跳转登录（副作用仅执行一次）。
 */
export function refreshAccessToken(): Promise<AuthTokens> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = runRefresh().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

async function runRefresh(): Promise<AuthTokens> {
  const ctx = authContext;
  if (!ctx || !ctx.getRefresh()) {
    ctx?.onRefreshFailed();
    ctx?.redirectToLogin(currentPath());
    throw new ApiError('UNAUTHORIZED', '登录状态已失效，请重新登录', 401);
  }
  try {
    return await ctx.refresh();
  } catch (err) {
    ctx.onRefreshFailed();
    ctx.redirectToLogin(currentPath());
    throw err;
  }
}

/** 重放标记：避免重放请求再次 401 时无限刷新。 */
interface RetryableConfig extends InternalAxiosRequestConfig {
  __isRetry?: boolean;
}

/** 统一 API 客户端：baseURL 默认 /api（dev 由 vite 代理到 bff），超时 120s。
 * 优先级：localStorage['ap.api-base']（刷新生效，用于 GitHub Pages 等静态托管指向可达后端）
 * > VITE_API_BASE（构建期）> '/api'（相对路径，dev/同源部署）。 */
export const RUNTIME_API_BASE_KEY = 'ap.api-base';

function resolveApiBase(): string {
  try {
    const stored = localStorage.getItem(RUNTIME_API_BASE_KEY)?.trim();
    if (stored) return stored.replace(/\/+$/, '');
  } catch {
    /* localStorage 不可用时退回构建期配置 */
  }
  return import.meta.env.VITE_API_BASE ?? '/api';
}

/** 当前生效的 API base（登录页「连接设置」预填/回显用）。 */
export function currentApiBase(): string {
  return resolveApiBase();
}

export const apiClient = axios.create({
  baseURL: resolveApiBase(),
  timeout: 120_000,
});

apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  config.headers.set('X-Request-Id', newRequestId());
  const access = authContext?.getAccess() ?? null;
  if (access) config.headers.set('Authorization', `Bearer ${access}`);
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RetryableConfig | undefined;
    const status = error.response?.status;
    // 401：刷新一次后重放原请求；/auth/login、/auth/refresh 与已重放请求不参与，防死循环。
    if (status === 401 && config && !config.__isRetry && !isAuthEndpoint(config.url) && authContext) {
      config.__isRetry = true;
      try {
        const tokens = await refreshAccessToken();
        config.headers.set('Authorization', `Bearer ${tokens.access}`);
        return apiClient.request(config);
      } catch {
        return Promise.reject(toApiError(error));
      }
    }
    return Promise.reject(toApiError(error));
  },
);

export default apiClient;
