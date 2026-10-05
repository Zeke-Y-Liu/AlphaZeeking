import { AddressInfo } from 'node:net';
import { createServer, Server } from 'node:http';

/** 一次被 mock core 收到的请求记录。 */
export interface MockCoreHit {
  method: string | undefined;
  url: string | undefined;
  headers: Record<string, string | string[] | undefined>;
  body: string;
}

export interface MockCore {
  baseUrl: string;
  hits: MockCoreHit[];
  close: () => Promise<void>;
}

/**
 * 起一个最小假 core（随机端口），用于离线、确定性的代理/限流测试。
 * - 路径含 `/error` 时返回 §6.3 统一错误体（409 VERIFY_GATE_FAILED）；
 * - `/api/v1/portfolios`、`/api/v1/data/status` 返回含「内部字段」的样例 JSON（供 T5.3 裁剪断言）；
 * - `/api/v1/runs/run_cached/*` 由上游显式设置 cache-control（供 T5.3 尊重上游断言）；
 * - 其余路径回显 method/url/body，供断言透传。
 */
export async function startMockCore(): Promise<MockCore> {
  const hits: MockCoreHit[] = [];

  const server: Server = createServer((req, res) => {
    let body = '';
    req.on('data', (chunk: Buffer) => {
      body += chunk.toString('utf8');
    });
    req.on('end', () => {
      hits.push({ method: req.method, url: req.url, headers: req.headers, body });
      const path = (req.url ?? '').split('?')[0];

      if ((req.url ?? '').startsWith('/api/v1/error')) {
        res.writeHead(409, { 'content-type': 'application/json' });
        res.end(
          JSON.stringify({
            code: 'VERIFY_GATE_FAILED',
            message: '数据验证未全绿',
            detail: { failed: ['golden_test'] },
          }),
        );
        return;
      }

      // 组合列表：故意携带内部字段（id/portfolio_id/strategy/artifact_keys 等）
      if (path === '/api/v1/portfolios' && req.method === 'GET') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(
          JSON.stringify({
            items: [
              {
                id: 42,
                pid: 'multi5',
                name: 'multi5 复刻组合',
                note: null,
                base_ccy: 'CNY',
                status: 'active',
                strategy: { reserved: true },
                created_at: '2026-01-01T00:00:00Z',
                positions: [
                  { id: 7, portfolio_id: 42, secid: '510300.SH', weight: 20.0 },
                  { id: 8, portfolio_id: 42, secid: '002014.SZ', weight: 80.0 },
                ],
                last_run: {
                  id: 9,
                  run_uid: 'run_20260930_1',
                  job_id: 'job_20260930_1',
                  artifact_keys: ['runs/run_20260930_1/report.html'],
                  summary: { ytd: 0.12, mdd: -0.08 },
                },
              },
            ],
            total: 1,
          }),
        );
        return;
      }

      // 数据状态：嵌套数组同样携带内部字段
      if (path === '/api/v1/data/status' && req.method === 'GET') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(
          JSON.stringify({
            universe: [
              {
                id: 1,
                secid: '510300.SH',
                name: '沪深300ETF',
                source: 'seed',
                last_bar: '2026-09-30',
              },
            ],
            verifications: [
              {
                id: 3,
                job_id: 'job_20260930_1',
                check: 'golden_test',
                status: 'pass',
                detail: { passed: 12 },
                created_at: '2026-09-30T12:00:00Z',
              },
            ],
            counts: { securities: 10, jobs: 3 },
            generated_at: '2026-10-01T00:00:00Z',
          }),
        );
        return;
      }

      // 上游已设置 cache-control：BFF 应尊重上游而非覆盖
      if (path.startsWith('/api/v1/runs/run_cached')) {
        res.writeHead(200, {
          'content-type': 'application/json',
          'cache-control': 'private, max-age=60',
        });
        res.end(JSON.stringify({ ok: true, run_uid: 'run_cached' }));
        return;
      }

      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(
        JSON.stringify({
          ok: true,
          method: req.method,
          path: req.url,
          body: body ? JSON.parse(body) : null,
        }),
      );
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', () => resolve()));
  const { port } = server.address() as AddressInfo;

  return {
    baseUrl: `http://127.0.0.1:${port}`,
    hits,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((err) => (err ? reject(err) : resolve())),
      ),
  };
}
