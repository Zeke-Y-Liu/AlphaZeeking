import { randomUUID } from 'node:crypto';

import { Injectable, Logger, NestMiddleware } from '@nestjs/common';
import { NextFunction, Request, Response } from 'express';

import { traceStore } from './trace-context';

/** 附加在 request 上的 traceId（供日志与异常过滤器复用）。 */
export interface RequestWithTrace extends Request {
  traceId?: string;
}

/**
 * traceId 中间件（T5.1）。
 * - 优先读取入站 X-Request-Id，否则 randomUUID()；
 * - 写入响应头、request 与 AsyncLocalStorage；
 * - 请求结束记录一行 method url status durationMs traceId。
 */
@Injectable()
export class TraceIdMiddleware implements NestMiddleware {
  private readonly logger = new Logger('HTTP');

  use(req: RequestWithTrace, res: Response, next: NextFunction): void {
    const incoming = req.headers['x-request-id'];
    const traceId =
      typeof incoming === 'string' && incoming.trim() !== '' ? incoming.trim() : randomUUID();

    // 统一透传给下游（core 客户端复用）与响应头
    req.traceId = traceId;
    req.headers['x-request-id'] = traceId;
    res.setHeader('X-Request-Id', traceId);

    const startedAt = Date.now();
    res.on('finish', () => {
      const durationMs = Date.now() - startedAt;
      this.logger.log(`${req.method} ${req.originalUrl} ${res.statusCode} ${durationMs}ms ${traceId}`);
    });

    traceStore.run({ traceId }, () => next());
  }
}
