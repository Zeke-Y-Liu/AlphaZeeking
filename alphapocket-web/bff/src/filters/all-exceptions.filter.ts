import {
  ArgumentsHost,
  Catch,
  ExceptionFilter,
  HttpException,
  Logger,
} from '@nestjs/common';
import { isAxiosError } from 'axios';
import { Response } from 'express';

/** 统一错误体（与 core §6.3 同构）。 */
export interface BffErrorBody {
  code: string;
  message: string;
  detail: unknown;
}

interface Mapping {
  status: number;
  body: BffErrorBody;
  level: 'warn' | 'error';
  stack?: string;
}

/**
 * 全局异常过滤器（T5.1）。
 * 规则见实施书 T5.1 与 §6.3：
 * - 下游错误体含 code/message → 原样透传（状态码与 body 均不变）；
 * - 下游不可达 → 502 UPSTREAM_UNAVAILABLE（detail 含目标 URL，不含堆栈）；
 * - Nest 自身 404 → NOT_FOUND；其余 HttpException 按状态码映射；
 * - 未捕获异常 → 500 INTERNAL_ERROR（堆栈只进日志）。
 */
@Catch()
export class AllExceptionsFilter implements ExceptionFilter {
  private readonly logger = new Logger('ExceptionFilter');

  catch(exception: unknown, host: ArgumentsHost): void {
    const ctx = host.switchToHttp();
    const res = ctx.getResponse<Response>();
    // 流式响应已开始写头（如产物下载中途断开）时无法改写，只能交由连接处理
    if (res.headersSent) return;

    const mapped = this.map(exception);

    const line = `${mapped.body.code} ${mapped.body.message}`;
    if (mapped.level === 'error') {
      this.logger.error(line, mapped.stack);
    } else {
      this.logger.warn(line);
    }

    res.status(mapped.status).json(mapped.body);
  }

  private map(exception: unknown): Mapping {
    // 1) 下游 core 的错误（含业务错误体或不可达）
    if (isAxiosError(exception)) {
      return this.mapAxios(exception);
    }
    // 2) Nest HttpException（含 ThrottlerException / NotFoundException 等）
    if (exception instanceof HttpException) {
      return this.mapHttp(exception);
    }
    // 3) 未捕获异常
    const stack = exception instanceof Error ? exception.stack : undefined;
    return {
      status: 500,
      body: { code: 'INTERNAL_ERROR', message: '内部服务器错误', detail: null },
      level: 'error',
      stack,
    };
  }

  private mapAxios(exception: import('axios').AxiosError): Mapping {
    const resp = exception.response;
    if (resp) {
      // 下游返回了响应：若已是统一错误体则原样透传
      if (this.hasCode(resp.data)) {
        return { status: resp.status, body: resp.data as BffErrorBody, level: 'warn' };
      }
      const status = resp.status;
      return {
        status,
        body: {
          code: this.codeForStatus(status),
          message: this.messageFrom(resp.data) ?? `上游返回 ${status}`,
          detail: null,
        },
        level: 'warn',
      };
    }
    // 无响应：连接被拒 / 超时 / DNS 失败等
    const target =
      (exception as import('axios').AxiosError & { upstreamUrl?: string }).upstreamUrl ??
      exception.config?.url ??
      '';
    return {
      status: 502,
      body: {
        code: 'UPSTREAM_UNAVAILABLE',
        message: '无法连接 core 上游服务',
        detail: { target, reason: exception.code ?? 'NETWORK_ERROR' },
      },
      level: 'error',
    };
  }

  private mapHttp(exception: HttpException): Mapping {
    const status = exception.getStatus();
    const resp = exception.getResponse();
    if (this.hasCode(resp)) {
      // 已是统一错误体（例如未来 core 错误被包装为 HttpException）→ 原样透传
      return { status, body: resp as BffErrorBody, level: 'warn' };
    }
    const message = this.messageFrom(resp) ?? exception.message;
    const detail = Array.isArray((resp as { message?: unknown })?.message)
      ? { errors: (resp as { message: unknown[] }).message }
      : null;
    return {
      status,
      body: { code: this.codeForStatus(status), message, detail },
      level: status >= 500 ? 'error' : 'warn',
    };
  }

  private hasCode(value: unknown): boolean {
    return (
      typeof value === 'object' &&
      value !== null &&
      typeof (value as { code?: unknown }).code === 'string'
    );
  }

  private messageFrom(value: unknown): string | undefined {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.join(', ');
    if (typeof value === 'object' && value !== null) {
      const message = (value as { message?: unknown }).message;
      if (typeof message === 'string') return message;
      if (Array.isArray(message)) return message.join(', ');
    }
    return undefined;
  }

  private codeForStatus(status: number): string {
    switch (status) {
      case 400:
      case 422:
        return 'VALIDATION_ERROR';
      case 401:
        return 'UNAUTHORIZED';
      case 403:
        return 'FORBIDDEN';
      case 404:
        return 'NOT_FOUND';
      case 405:
        return 'METHOD_NOT_ALLOWED';
      case 409:
        return 'CONFLICT';
      case 429:
        return 'TOO_MANY_REQUESTS';
      case 502:
        return 'UPSTREAM_UNAVAILABLE';
      case 503:
        return 'SERVICE_UNAVAILABLE';
      default:
        if (status >= 500) return 'INTERNAL_ERROR';
        if (status >= 400) return 'BAD_REQUEST';
        return 'ERROR';
    }
  }
}
