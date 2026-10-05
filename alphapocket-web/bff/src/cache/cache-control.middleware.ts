import { Injectable, NestMiddleware } from '@nestjs/common';
import { NextFunction, Request, Response } from 'express';

import { matchRoute } from '../routing/path-match';

/**
 * 不可变资源缓存策略（T5.3）。
 * `GET /api/v1/runs/:run_uid` 及其子资源（产物元数据 / 产物流）一旦生成即不可变，
 * 设置长缓存 `public, max-age=31536000, immutable`。
 *
 * 与上游的关系：本中间件先于代理设置该头；通配代理会在转发阶段把 core 的
 * `cache-control`（若存在）回写覆盖，因此**上游已设置时尊重上游**，本策略仅兜底。
 */
const IMMUTABLE_CACHE_CONTROL = 'public, max-age=31536000, immutable';

const RUN_RESOURCE_ROUTES = [
  { method: 'GET', pattern: '/api/v1/runs/:run_uid' },
  { method: 'GET', pattern: '/api/v1/runs/:run_uid/*' },
];

@Injectable()
export class CacheControlMiddleware implements NestMiddleware {
  use(req: Request, res: Response, next: NextFunction): void {
    // 注意：Nest 的 forRoutes('*') 以正则挂载中间件，此阶段 req.url/req.path 已被剥离为
    // 相对路径，必须用 originalUrl 取完整请求路径。
    const pathname = (req.originalUrl ?? req.url ?? '').split('?')[0];
    if (matchRoute(req.method, pathname, RUN_RESOURCE_ROUTES)) {
      res.setHeader('Cache-Control', IMMUTABLE_CACHE_CONTROL);
    }
    next();
  }
}
