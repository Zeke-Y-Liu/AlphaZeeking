import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { AppConfigService } from '../src/config/app-config.service';

describe('HealthController (e2e)', () => {
  let app: INestApplication;

  beforeAll(async () => {
    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
      // 限流上限调到 2，用于证明 /healthz 豁免；core 指向不可达端口，healthz 仍须 200
      .overrideProvider(AppConfigService)
      .useValue({
        coreUrl: 'http://127.0.0.1:1',
        port: 0,
        jwtSecret: '',
        jwtAccessTtl: '15m',
        jwtRefreshTtl: '7d',
        throttleTtlSeconds: 60,
        throttleLimit: 2,
      })
      .compile();

    app = moduleRef.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    await app.close();
  });

  it('GET /healthz -> 200 且 status=ok（core 不可达时仅标 degraded）', async () => {
    const res = await request(app.getHttpServer()).get('/healthz').expect(200);
    expect(res.body.status).toBe('ok');
    expect(['ok', 'degraded']).toContain(res.body.core);
    expect(res.body.core).toBe('degraded');
  });

  it('/healthz 豁免限流：连续 6 次（> limit 2）仍全部 200', async () => {
    for (let i = 0; i < 6; i += 1) {
      await request(app.getHttpServer()).get('/healthz').expect(200);
    }
  });
});
