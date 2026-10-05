import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { AppConfigService } from '../src/config/app-config.service';
import { MockCore, startMockCore } from './mock-core';
import { TEST_JWT_SECRET, signToken } from './token';

/** DTO 校验层 e2e（T5.3）：mock core 回显，断言到达 core 的 body 已被裁剪。 */
describe('DTO 校验（T5.3, e2e, mock core）', () => {
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

  it('POST /portfolios：多余字段被剥离后才到达 core', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/v1/portfolios')
      .set('Authorization', bearer())
      .send({
        name: 'multi5 复刻组合',
        pid: 'multi5',
        equal_weight: true,
        hack: 'should-be-stripped',
        __debug: { a: 1 },
        positions: [
          { secid: '510300.SH', weight: 20, extra: 'x' },
          { secid: '002014.SZ', weight: 80 },
        ],
      })
      .expect(200);

    expect(res.body.body).toEqual({
      name: 'multi5 复刻组合',
      pid: 'multi5',
      equal_weight: true,
      positions: [
        { secid: '510300.SH', weight: 20 },
        { secid: '002014.SZ', weight: 80 },
      ],
    });
    // 到达 core 的真实 body 亦不含多余字段
    const hit = core.hits.at(-1);
    expect(hit?.body).not.toContain('should-be-stripped');
    expect(JSON.parse(hit?.body ?? '{}')).not.toHaveProperty('hack');
  });

  it('POST /portfolios：必填 name 缺失 → 422 且 detail 字段级', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/v1/portfolios')
      .set('Authorization', bearer())
      .send({ positions: [{ secid: '510300.SH', weight: 100 }] })
      .expect(422);

    expect(res.body.code).toBe('VALIDATION_ERROR');
    expect(res.body.message).toBe('请求参数校验失败');
    expect(res.body.detail.errors).toEqual(
      expect.arrayContaining([expect.objectContaining({ field: 'name' })]),
    );
  });

  it('POST /portfolios：name 为空串 → 422', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/v1/portfolios')
      .set('Authorization', bearer())
      .send({ name: '   ', positions: [{ secid: '510300.SH', weight: 100 }] })
      .expect(422);
    expect(res.body.code).toBe('VALIDATION_ERROR');
  });

  it('POST /portfolios：positions 项类型非法 → 422 且含 positions[0].weight', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/v1/portfolios')
      .set('Authorization', bearer())
      .send({ name: 'x', positions: [{ secid: '510300.SH', weight: '20' }] })
      .expect(422);

    expect(res.body.detail.errors).toEqual(
      expect.arrayContaining([expect.objectContaining({ field: 'positions[0].weight' })]),
    );
  });

  it('POST /portfolios/:id/backtest：rebalance 枚举越界 → 422', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/v1/portfolios/42/backtest')
      .set('Authorization', bearer())
      .send({ rebalance: 'weekly' })
      .expect(422);

    expect(res.body.code).toBe('VALIDATION_ERROR');
    expect(res.body.detail.errors).toEqual(
      expect.arrayContaining([expect.objectContaining({ field: 'rebalance' })]),
    );
  });

  it('POST /portfolios/:id/backtest：合法枚举 + 多余字段剥离', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/v1/portfolios/42/backtest')
      .set('Authorization', bearer())
      .send({
        start: '2025-01-01',
        end: '2025-12-31',
        rf: 0.02,
        rebalance: 'monthly',
        benchmark_secid: '000300.SH',
        sneaky: true,
      })
      .expect(200);

    expect(res.body.body).toEqual({
      start: '2025-01-01',
      end: '2025-12-31',
      rf: 0.02,
      rebalance: 'monthly',
      benchmark_secid: '000300.SH',
    });
  });

  it('未认证 + 非法 body → 先 401（校验在 AuthGuard 之后）', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/v1/portfolios')
      .send({ positions: 'not-an-array' })
      .expect(401);
    expect(res.body.code).toBe('UNAUTHORIZED');
  });
});
