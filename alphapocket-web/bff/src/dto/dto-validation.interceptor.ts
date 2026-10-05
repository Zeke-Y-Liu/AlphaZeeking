import {
  CallHandler,
  ExecutionContext,
  HttpException,
  HttpStatus,
  Injectable,
  NestInterceptor,
} from '@nestjs/common';
import { Request } from 'express';
import { Observable } from 'rxjs';

import { matchPath } from '../routing/path-match';
import { DTO_ROUTES } from './contracts';
import { validateObject } from './validation';

/**
 * DTO 校验拦截器（T5.3）。
 *
 * 以 `APP_INTERCEPTOR` 全局挂载，仅在命中 `DTO_ROUTES`（组合创建 / 回测参数）时生效：
 * - 白名单裁剪：多余字段从 `req.body` 中剥离后再交给通配代理转发；
 * - 已知字段类型 / 枚举非法 → 抛 422，错误体同构 §6.3 `{code,message,detail}`，
 *   其中 `detail.errors` 为字段级明细。
 *
 * 用拦截器而非中间件的原因：拦截器在全局 AuthGuard 之后执行，
 * 未经认证的请求先得到 401，不会先暴露校验细节。
 */
@Injectable()
export class DtoValidationInterceptor implements NestInterceptor {
  intercept(context: ExecutionContext, next: CallHandler): Observable<unknown> {
    const req = context.switchToHttp().getRequest<Request>();
    const pathname = (req.originalUrl ?? req.url ?? '').split('?')[0];

    const route = DTO_ROUTES.find(
      (candidate) => candidate.method === req.method && matchPath(pathname, candidate.pattern),
    );
    if (route) {
      const { value, errors } = validateObject(req.body, route.schema);
      if (errors.length > 0) {
        throw new HttpException(
          {
            code: 'VALIDATION_ERROR',
            message: '请求参数校验失败',
            detail: { errors },
          },
          HttpStatus.UNPROCESSABLE_ENTITY,
        );
      }
      req.body = value; // 干净 body 回写，代理原样转发
    }

    return next.handle();
  }
}
