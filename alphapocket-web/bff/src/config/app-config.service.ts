import { Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';

/**
 * 类型化配置读取门面（T5.1）。
 * 所有键都在此集中暴露，供 core 客户端、限流、以及后续 T5.2（JWT）复用。
 */
@Injectable()
export class AppConfigService {
  constructor(private readonly config: ConfigService) {}

  /** core 上游基址，如 http://localhost:8082 */
  get coreUrl(): string {
    return this.config.get<string>('coreUrl') ?? 'http://localhost:8082';
  }

  /** BFF 监听端口 */
  get port(): number {
    return Number(this.config.get<number>('port') ?? 8081);
  }

  /** JWT 密钥（T5.2 校验用）；缺省与 core 保持一致，无 .env 时可互通 */
  get jwtSecret(): string {
    return this.config.get<string>('jwt.secret') ?? 'dev-only-change-me';
  }

  /** access token TTL（T5.2） */
  get jwtAccessTtl(): string {
    return this.config.get<string>('jwt.accessTtl') ?? '15m';
  }

  /** refresh token TTL（T5.2） */
  get jwtRefreshTtl(): string {
    return this.config.get<string>('jwt.refreshTtl') ?? '7d';
  }

  /** 限流窗口（秒） */
  get throttleTtlSeconds(): number {
    return Number(this.config.get<number>('throttle.ttlSeconds') ?? 60);
  }

  /** 限流窗口内每 IP 最大请求数 */
  get throttleLimit(): number {
    return Number(this.config.get<number>('throttle.limit') ?? 100);
  }

  /** CORS 额外白名单（逗号分隔 origin，精确匹配）；用于 GitHub Pages 等跨源静态托管 */
  get corsOrigins(): string[] {
    return (this.config.get<string[]>('corsOrigins') ?? []).filter(Boolean);
  }
}
