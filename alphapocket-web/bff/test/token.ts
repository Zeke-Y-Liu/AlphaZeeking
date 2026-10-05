import { JwtService } from '@nestjs/jwt';

/** 测试用 JWT 密钥（须与 AppConfigService override 中的 jwtSecret 一致）。 */
export const TEST_JWT_SECRET = 'test-jwt-secret';

export interface SignOptions {
  secret?: string;
  ttl?: string | number;
  typ?: string;
  sub?: string;
  uid?: number;
  role?: string;
}

/**
 * 用与 core 相同的规格（HS256）自签一个 JWT，供 BFF 守卫测试使用（离线、确定性）。
 * `ttl` 传负数即生成已过期令牌。
 */
export function signToken(options: SignOptions = {}): string {
  const {
    secret = TEST_JWT_SECRET,
    ttl = '15m',
    typ = 'access',
    sub = 'demo',
    uid = 1,
    role = 'owner',
  } = options;
  return new JwtService({ secret }).sign(
    { sub, uid, role, typ },
    { algorithm: 'HS256', expiresIn: ttl },
  );
}
