import { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import { Server, createServer } from 'node:http';
import { AddressInfo } from 'node:net';
import request from 'supertest';

import { AppModule } from '../src/app.module';
import { configureCors } from '../src/auth/cors';
import { AppConfigService } from '../src/config/app-config.service';
import { TEST_JWT_SECRET, signToken } from './token';

interface MockAuthCore {
  baseUrl: string;
  close: () => Promise<void>;
}

/** 认证三件套（core 规格，供透传断言）。 */
function tokenBundle(): Record<string, unknown> {
  return {
    access: 'ACCESS.JWT.TOKEN',
    refresh: 'REFRESH.JWT.TOKEN',
    token_type: 'bearer',
    expires_in: 900,
    user: { id: 1, username: 'demo', role: 'owner' },
  };
}

/**
 * 最小 auth mock core（随机端口，离线、确定性）：
 * - login：demo/alphapocket → 200 三件套；locked → 429；其余 → 401 统一错误体；
 * - refresh → 200 三件套；me → 200 用户；其它（业务代理）→ 200 回显。
 */
async function startAuthMockCore(): Promise<MockAuthCore> {
  const server: Server = createServer((req, res) => {
    let body = '';
    req.on('data', (chunk: Buffer) => (body += chunk.toString('utf8')));
    req.on('end', () => {
      const path = (req.url ?? '').split('?')[0];
      const send = (status: number, payload: unknown): void => {
        res.writeHead(status, { 'content-type': 'application/json' });
        res.end(JSON.stringify(payload));
      };

      if (path === '/api/v1/auth/login') {
        const data = body ? JSON.parse(body) : {};
        if (data.username === 'demo' && data.password === 'alphapocket') {
          send(200, tokenBundle());
          return;
        }
        if (data.username === 'locked') {
          send(429, {
            code: 'ACCOUNT_LOCKED',
            message: '账号已锁定，请 15 分钟后重试',
            detail: null,
          });
          return;
        }
        send(401, { code: 'UNAUTHORIZED', message: '用户名或密码错误', detail: null });
        return;
      }
      if (path === '/api/v1/auth/refresh') {
        send(200, tokenBundle());
        return;
      }
      if (path === '/api/v1/auth/me') {
        send(200, { id: 1, username: 'demo', role: 'owner' });
        return;
      }
      send(200, { ok: true, path, method: req.method });
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', () => resolve()));
  const { port } = server.address() as AddressInfo;
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((err) => (err ? reject(err) : resolve())),
      ),
  };
}

describe('Auth (e2e, mock core)', () => {
  let app: INestApplication;
  let core: MockAuthCore;

  beforeAll(async () => {
    core = await startAuthMockCore();
    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
      .overrideProvider(AppConfigService)
      .useValue({
        coreUrl: core.baseUrl,
        port: 0,
        jwtSecret: TEST_JWT_SECRET,
        jwtAccessTtl: '15m',
        jwtRefreshTtl: '7d',
        throttleTtlSeconds: 60,
        throttleLimit: 1000, // 认证测试不限流
      })
      .compile();

    app = moduleRef.createNestApplication();
    configureCors(app);
    await app.init();
  });

  afterAll(async () => {
    await app.close();
    await core.close().catch(() => undefined);
  });

  it('POST /api/auth/login 成功：透传 token 三件套与 user', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/auth/login')
      .send({ username: 'demo', password: 'alphapocket' })
      .expect(200);

    expect(res.body).toEqual(tokenBundle());
  });

  it('core 401 → BFF 原样透传状态码与统一错误体', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/auth/login')
      .send({ username: 'demo', password: 'bad' })
      .expect(401);

    expect(res.body).toEqual({
      code: 'UNAUTHORIZED',
      message: '用户名或密码错误',
      detail: null,
    });
  });

  it('core 429 → BFF 原样透传 ACCOUNT_LOCKED', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/auth/login')
      .send({ username: 'locked', password: 'whatever' })
      .expect(429);

    expect(res.body.code).toBe('ACCOUNT_LOCKED');
    expect(typeof res.body.message).toBe('string');
  });

  it('GET /api/auth/me 带 access → 转发 core 并原样返回', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/auth/me')
      .set('Authorization', `Bearer ${signToken()}`)
      .expect(200);

    expect(res.body).toEqual({ id: 1, username: 'demo', role: 'owner' });
  });

  it('无 token 访问 /api/v1/portfolios → 401 UNAUTHORIZED', async () => {
    const res = await request(app.getHttpServer()).get('/api/v1/portfolios').expect(401);
    expect(res.body).toEqual({
      code: 'UNAUTHORIZED',
      message: expect.any(String),
      detail: null,
    });
  });

  it('带有效 access → 守卫放行，到达代理', async () => {
    const res = await request(app.getHttpServer())
      .get('/api/v1/portfolios')
      .set('Authorization', `Bearer ${signToken()}`)
      .expect(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.path).toBe('/api/v1/portfolios');
  });

  it('过期 access → 401 UNAUTHORIZED', async () => {
    const expired = signToken({ ttl: -10 });
    const res = await request(app.getHttpServer())
      .get('/api/v1/portfolios')
      .set('Authorization', `Bearer ${expired}`)
      .expect(401);
    expect(res.body.code).toBe('UNAUTHORIZED');
  });

  it('白名单 /healthz 无需 token', async () => {
    const res = await request(app.getHttpServer()).get('/healthz').expect(200);
    expect(res.body.status).toBe('ok');
  });

  it('CORS 预检 OPTIONS /api/auth/login 带 Origin → 返回 Allow-Origin', async () => {
    const res = await request(app.getHttpServer())
      .options('/api/auth/login')
      .set('Origin', 'http://localhost:5173')
      .set('Access-Control-Request-Method', 'POST');

    expect(res.headers['access-control-allow-origin']).toBe('http://localhost:5173');
    expect(res.headers['access-control-allow-headers']).toContain('Authorization');
  });
});
