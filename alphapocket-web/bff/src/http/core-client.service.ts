import { HttpService } from '@nestjs/axios';
import { Injectable } from '@nestjs/common';
import { AxiosRequestConfig, AxiosResponse } from 'axios';
import { Observable, throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';

import { AppConfigService } from '../config/app-config.service';
import { traceStore } from '../middleware/trace-context';

/**
 * core 上游薄封装（T5.1）。
 * - 统一拼 `${CORE_URL}`（传入绝对 URL 时原样使用）；
 * - 自动透传当前请求的 X-Request-Id（来自 traceStore）；
 * - 出错时在异常上附加 upstreamUrl，供全局过滤器生成 detail（不含堆栈）。
 */
@Injectable()
export class CoreClientService {
  constructor(
    private readonly http: HttpService,
    private readonly config: AppConfigService,
  ) {}

  private toAbsoluteUrl(url: string): string {
    if (/^https?:\/\//i.test(url)) return url;
    const base = this.config.coreUrl.replace(/\/+$/, '');
    return `${base}${url.startsWith('/') ? url : `/${url}`}`;
  }

  private withTrace(config: AxiosRequestConfig): AxiosRequestConfig {
    const traceId = traceStore.getStore()?.traceId;
    const headers: Record<string, unknown> = { ...(config.headers as Record<string, unknown>) };
    if (traceId && !headers['X-Request-Id'] && !headers['x-request-id']) {
      headers['X-Request-Id'] = traceId;
    }
    return { ...config, headers: headers as AxiosRequestConfig['headers'] };
  }

  /** 通用请求：自动补全基址与 traceId。 */
  request<T = unknown>(config: AxiosRequestConfig): Observable<AxiosResponse<T>> {
    const merged = this.withTrace(config);
    const url = merged.url ?? '';
    return this.http.request<T>({ ...merged, url: this.toAbsoluteUrl(url) }).pipe(
      catchError((err: unknown) => {
        if (err && typeof err === 'object') {
          (err as { upstreamUrl?: string }).upstreamUrl = this.toAbsoluteUrl(url);
        }
        return throwError(() => err);
      }),
    );
  }

  get<T = unknown>(url: string, config: AxiosRequestConfig = {}): Observable<AxiosResponse<T>> {
    return this.request<T>({ ...config, method: 'GET', url });
  }

  post<T = unknown>(
    url: string,
    data?: unknown,
    config: AxiosRequestConfig = {},
  ): Observable<AxiosResponse<T>> {
    return this.request<T>({ ...config, method: 'POST', url, data });
  }

  put<T = unknown>(
    url: string,
    data?: unknown,
    config: AxiosRequestConfig = {},
  ): Observable<AxiosResponse<T>> {
    return this.request<T>({ ...config, method: 'PUT', url, data });
  }

  delete<T = unknown>(url: string, config: AxiosRequestConfig = {}): Observable<AxiosResponse<T>> {
    return this.request<T>({ ...config, method: 'DELETE', url });
  }
}
