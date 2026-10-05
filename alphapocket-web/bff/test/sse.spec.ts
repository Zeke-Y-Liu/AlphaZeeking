import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { AppConfigService } from '../src/config/app-config.service';
import { MockCore, startMockCore } from './mock-core';
import { TEST_JWT_SECRET, signToken } from './token';

/** SSE 端点占位 e2e（T5.3）：返回 501，错误体同构 §6.3。 */
describe('SSE 端点占位（T5.3, e2e, mock core）', () => {
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
        throttleLimit: 1000,
      })
      .compile();

    app = moduleRef.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    await app.close();
    await core.close().catch(() => undefined);
  });

  const bearer = (): string => `Bearer ${signToken()}`;

  it('GET /jobs/:id/events → 501 NOT_IMPLEMENTED，错误体同构', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/jobs/job_20260930_1/events')
      .set('Authorization', bearer())
      .expect(501);

    expect(res.body).toEqual({
      code: 'NOT_IMPLEMENTED',
      message: expect.stringContaining('SSE'),
      detail: null,
    });
    // 未到达 core（无命中记录）
    expect(core.hits.some((hit) => (hit.url ?? '').includes('/events'))).toBe(false);
  });

  it('未认证访问该端点 → 401（守卫优先）', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/jobs/job_1/events')
      .expect(401);
    expect(res.body.code).toBe('UNAUTHORIZED');
  });
});
