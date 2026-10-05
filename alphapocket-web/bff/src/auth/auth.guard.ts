import {
  CanActivate,
  ExecutionContext,
  Injectable,
  UnauthorizedException,
} from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
import { Request } from 'express';

/** 白名单：精确匹配，无需令牌。 */
const WHITELIST = new Set(['/healthz', '/api/auth/login', '/api/auth/refresh']);

interface AccessPayload {
  sub?: string;
  uid?: number;
  role?: string;
  typ?: string;
}

function unauthorized(message: string): UnauthorizedException {
  // 统一错误体（§6.3）：{code,message,detail}，由全局过滤器原样输出
  return new UnauthorizedException({ code: 'UNAUTHORIZED', message, detail: null });
}

/**
 * 全局认证守卫（T5.2，APP_GUARD）。
 * 除白名单外，一律要求 `Authorization: Bearer <access>`；
 * 使用 `AP_JWT_SECRET` 本地 HS256 验签，且 `typ === 'access'`；
 * 缺失/无效/过期/类型不符 → 401 统一错误体。
 */
@Injectable()
export class AuthGuard implements CanActivate {
  constructor(private readonly jwt: JwtService) {}

  canActivate(context: ExecutionContext): boolean {
    const request = context.switchToHttp().getRequest<Request>();
    const path = (request.originalUrl ?? request.url ?? '').split('?')[0];
    if (WHITELIST.has(path)) return true;

    const header = request.headers['authorization'];
    if (!header || !header.startsWith('Bearer ')) {
      throw unauthorized('缺少访问令牌');
    }
    const token = header.slice('Bearer '.length).trim();
    if (!token) throw unauthorized('缺少访问令牌');

    let payload: AccessPayload;
    try {
      payload = this.jwt.verify<AccessPayload>(token, { algorithms: ['HS256'] });
    } catch {
      throw unauthorized('访问令牌无效或已过期');
    }
    if (payload?.typ !== 'access') {
      throw unauthorized('访问令牌类型不符');
    }
    (request as Request & { user?: AccessPayload }).user = payload;
    return true;
  }
}
