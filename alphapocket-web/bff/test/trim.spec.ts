import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { AppConfigService } from '../src/config/app-config.service';
import { MockCore, startMockCore } from './mock-core';
import { TEST_JWT_SECRET, signToken } from './token';

/** 响应裁剪 e2e（T5.3）：仅两个 GET 路径缓冲裁剪，其余仍流式透传。 */
describe('响应裁剪（T5.3, e2e, mock core）', () => {
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

  it('GET /portfolios：顶层与嵌套内部字段均被剔除', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/portfolios')
      .set('Authorization', bearer())
      .expect(200);

    expect(res.body).toEqual({
      items: [
        {
          pid: 'multi5',
          name: 'multi5 复刻组合',
          note: null,
          base_ccy: 'CNY',
          status: 'active',
          created_at: '2026-01-01T00:00:00Z',
          positions: [
            { secid: '510300.SH', weight: 20 },
            { secid: '002014.SZ', weight: 80 },
          ],
          last_run: {
            run_uid: 'run_20260930_1',
            job_id: 'job_20260930_1',
            summary: { ytd: 0.12, mdd: -0.08 },
          },
        },
      ],
      total: 1,
    });
    // 明确断言被剔除的键不存在
    const raw = JSON.stringify(res.body);
    for (const key of ['"id"', '"portfolio_id"', '"strategy"', '"artifact_keys"']) {
      expect(raw).not.toContain(key);
    }
  });

  it('GET /data/status：嵌套数组内部字段被剔除，业务字段保留', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/data/status')
      .set('Authorization', bearer())
      .expect(200);

    expect(res.body.universe).toEqual([
      { secid: '510300.SH', name: '沪深300ETF', source: 'seed', last_bar: '2026-09-30' },
    ]);
    expect(res.body.verifications).toEqual([
      {
        job_id: 'job_20260930_1',
        check: 'golden_test',
        status: 'pass',
        detail: { passed: 12 },
        created_at: '2026-09-30T12:00:00Z',
      },
    ]);
    expect(res.body.counts).toEqual({ securities: 10, jobs: 3 });
    expect(JSON.stringify(res.body)).not.toContain('"id"');
  });

  it('其余路径不受影响：仍原样流式透传（含内部字段）', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/_anything')
      .set('Authorization', bearer())
      .expect(200);
    expect(res.body).toEqual({
      ok: true,
      method: 'GET',
      path: '/api/v1/_anything',
      body: null,
    });
  });
});
