import { Controller, Get } from '@nestjs/common';
import { SkipThrottle } from '@nestjs/throttler';
import { firstValueFrom } from 'rxjs';

import { CoreClientService } from './http/core-client.service';

/**
 * 存活探针（T5.1）。
 * - 豁免限流（@SkipThrottle）；
 * - 附带 core 可达性字段：探测失败不得使自身非 200，只标 degraded。
 */
@Controller()
export class HealthController {
  constructor(private readonly core: CoreClientService) {}

  @SkipThrottle()
  @Get('healthz')
  async healthz(): Promise<{ status: string; core: 'ok' | 'degraded' }> {
    let core: 'ok' | 'degraded' = 'degraded';
    try {
      await firstValueFrom(
        this.core.get('/healthz', { timeout: 800, validateStatus: () => true }),
      );
      core = 'ok';
    } catch {
      core = 'degraded';
    }
    return { status: 'ok', core };
  }
}
