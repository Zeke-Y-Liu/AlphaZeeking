import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { AppConfigService } from '../src/config/app-config.service';
import { MockCore, startMockCore } from './mock-core';
import { TEST_JWT_SECRET, signToken } from './token';

describe('Throttler (e2e)', () => {
  let app: INestApplication;
  let core: MockCore;

  beforeAll(async () => {
    core = await startMockCore();
    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
      .overrideProvider(AppConfigService)
      .useValue({
        coreUrl: core.baseUrl,
        port: 0,
        jwtSecret: TEST_JWT_SECRET,
        jwtAccessTtl: '15m',
        jwtRefreshTtl: '7d',
        throttleTtlSeconds: 60,
        throttleLimit: 3, // 调小以便快速触发 429
      })
      .compile();

    app = moduleRef.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    await app.close();
    await core.close().catch(() => undefined);
  });

  it('超过 limit 后返回 429 且 body 为统一错误体 code=TOO_MANY_REQUESTS', async () => {
    // T5.2 起 /api/v1/* 受全局 AuthGuard 保护，需携带有效 access token
    const bearer = `Bearer ${signToken()}`;
    for (let i = 0; i < 3; i += 1) {
      await request(app.getHttpServer())
        .get('/api/v1/t')
        .set('Authorization', bearer)
        .expect(200);
    }
    const res = await request(app.getHttpServer())
      .get('/api/v1/t')
      .set('Authorization', bearer)
      .expect(429);
    expect(res.body.code).toBe('TOO_MANY_REQUESTS');
    expect(typeof res.body.message).toBe('string');
  });
});
