import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { AppConfigService } from '../src/config/app-config.service';
import { MockCore, startMockCore } from './mock-core';
import { TEST_JWT_SECRET, signToken } from './token';

const IMMUTABLE = 'public, max-age=31536000, immutable';

/** Cache-Control 策略 e2e（T5.3）。 */
describe('Cache-Control 策略（T5.3, e2e, mock core）', () => {
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

  it('GET /runs/:run_uid → immutable', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/runs/run_immutable')
      .set('Authorization', bearer())
      .expect(200);
    expect(res.headers['cache-control']).toBe(IMMUTABLE);
  });

  it('GET /runs/:run_uid/artifacts/:file → immutable', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/runs/run_immutable/artifacts/report.html')
      .set('Authorization', bearer())
      .expect(200);
    expect(res.headers['cache-control']).toBe(IMMUTABLE);
  });

  it('上游已给 cache-control 时尊重上游（不覆盖）', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/runs/run_cached')
      .set('Authorization', bearer())
      .expect(200);
    expect(res.headers['cache-control']).toBe('private, max-age=60');
  });

  it('run 列表 /runs 不套用 immutable', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/runs?limit=20')
      .set('Authorization', bearer())
      .expect(200);
    expect(res.headers['cache-control']).toBeUndefined();
  });
});
