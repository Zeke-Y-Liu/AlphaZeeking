import { Injectable, NestMiddleware } from '@nestjs/common';
import { NextFunction, Request, Response } from 'express';

import { matchRoute } from '../routing/path-match';
import { stripInternalFields } from './internal-fields';

/**
 * 需要缓冲并裁剪 JSON 响应的只读端点（T5.3）。
 * 仅这两个精确 GET 路径；其余（含 run 产物流）保持流式透传不动。
 */
const TRIM_ROUTES = [
  { method: 'GET', pattern: '/api/v1/portfolios' },
  { method: 'GET', pattern: '/api/v1/data/status' },
];

function toBuffer(value: unknown, encoding?: unknown): Buffer | undefined {
  if (value === undefined || value === null) return undefined;
  if (Buffer.isBuffer(value)) return value;
  const enc = typeof encoding === 'string' ? (encoding as BufferEncoding) : undefined;
  return Buffer.from(value as string, enc);
}

function trimJson(raw: Buffer): Buffer | undefined {
  try {
    const parsed: unknown = JSON.parse(raw.toString('utf8'));
    return Buffer.from(JSON.stringify(stripInternalFields(parsed)), 'utf8');
  } catch {
    return undefined; // 非 JSON 或解析失败：原样放行
  }
}

/**
 * 响应裁剪中间件（T5.3）。
 *
 * 通配代理当前以 `responseType:'stream'` 回传，无法直接改写响应体。
 * 对上述两个 GET 路径，本中间件在其之前接管 `res.write/res.end`，把上游 JSON
 * 缓冲下来、剔除内部字段后再写出；其余路径不进入本分支，完全保持流式。
 *
 * - 仅对 2xx 且 content-type 含 json 的响应裁剪；错误体与二进制原样放行；
 * - 缓冲后由 Node 重新计算 Content-Length（不转发上游 content-length）。
 */
@Injectable()
export class ResponseTrimMiddleware implements NestMiddleware {
  use(req: Request, res: Response, next: NextFunction): void {
    // 中间件以正则挂载（forRoutes('*')），此阶段 req.url/req.path 为相对路径，
    // 用 originalUrl 取完整路径。
    const pathname = (req.originalUrl ?? req.url ?? '').split('?')[0];
    if (!matchRoute(req.method, pathname, TRIM_ROUTES)) {
      next();
      return;
    }

    const chunks: Buffer[] = [];
    const originalEnd = res.end.bind(res);

    res.write = ((chunk: unknown, encoding?: unknown, callback?: unknown): boolean => {
      const buf = toBuffer(chunk, encoding);
      if (buf) chunks.push(buf);
      if (typeof callback === 'function') (callback as () => void)();
      return true;
    }) as Response['write'];

    res.end = ((chunk?: unknown, encoding?: unknown, callback?: unknown): Response => {
      const buf = toBuffer(chunk, encoding);
      if (buf) chunks.push(buf);
      const raw = Buffer.concat(chunks);
      const done =
        typeof encoding === 'function'
          ? (encoding as () => void)
          : typeof callback === 'function'
            ? (callback as () => void)
            : undefined;

      const contentType = String(res.getHeader('content-type') ?? '');
      const trimmed = res.statusCode < 400 && contentType.includes('json') ? trimJson(raw) : undefined;

      if (trimmed) {
        res.removeHeader('content-length');
        if (done) originalEnd(trimmed, done);
        else originalEnd(trimmed);
        return res;
      }
      if (done) originalEnd(raw, done);
      else originalEnd(raw);
      return res;
    }) as Response['end'];

    next();
  }
}
