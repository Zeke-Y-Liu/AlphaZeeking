import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { AppConfigService } from '../src/config/app-config.service';
import { MockCore, startMockCore } from './mock-core';
import { TEST_JWT_SECRET, signToken } from './token';

describe('ProxyController (e2e, mock core)', () => {
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
        throttleLimit: 1000, // 代理测试不限流
      })
      .compile();

    app = moduleRef.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    await app.close();
    await core.close().catch(() => undefined);
  });

  // T5.2 起 /api/v1/* 业务代理受全局 AuthGuard 保护，测试需携带有效 access token
  const bearer = (): string => `Bearer ${signToken()}`;

  it('GET 透传：路径含 query、状态码 200、body 原样，且响应头含 X-Request-Id', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/_anything?x=1&y=hello')
      .set('Authorization', bearer())
      .expect(200);

    expect(res.headers['x-request-id']).toBeTruthy();
    expect(res.body).toEqual({
      ok: true,
      method: 'GET',
      path: '/api/v1/_anything?x=1&y=hello',
      body: null,
    });
    // 下游确实收到了同一路径与 query
    const hit = core.hits.at(-1);
    expect(hit?.url).toBe('/api/v1/_anything?x=1&y=hello');
  });

  it('POST body 透传', async () => {
    const payload = { a: 1, b: 'x' };
    const res = await request(app.getHttpServer())
      .post('/api/v1/things')
      .set('Authorization', bearer())
      .send(payload)
      .expect(200);

    expect(res.body.method).toBe('POST');
    expect(res.body.path).toBe('/api/v1/things');
    expect(res.body.body).toEqual(payload);
  });

  it('下游统一错误体原样透传：409 VERIFY_GATE_FAILED 与 body 不变', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/error')
      .set('Authorization', bearer())
      .expect(409);

    expect(res.body).toEqual({
      code: 'VERIFY_GATE_FAILED',
      message: '数据验证未全绿',
      detail: { failed: ['golden_test'] },
    });
  });

  it('入站带 X-Request-Id 时透传到下游', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/rid')
      .set('Authorization', bearer())
      .set('X-Request-Id', 'abc-123')
      .expect(200);

    expect(res.headers['x-request-id']).toBe('abc-123');
    const hit = core.hits.at(-1);
    expect(hit?.headers['x-request-id']).toBe('abc-123');
  });

  // 放在最后：关闭假 core 后断言 502（不影响前面的用例）
  it('下游不可达 -> 502 UPSTREAM_UNAVAILABLE（detail 含目标 URL，不含堆栈）', async () => {
    await core.close();
    const res = await request(app.getHttpServer())
      .get('/api/v1/x')
      .set('Authorization', bearer())
      .expect(502);

    expect(res.body.code).toBe('UPSTREAM_UNAVAILABLE');
    expect(res.body.message).toContain('core');
    expect(res.body.detail).toEqual({
      target: `${core.baseUrl}/api/v1/x`,
      reason: expect.any(String),
    });
    expect(res.body.detail.stack).toBeUndefined();
  });
});
