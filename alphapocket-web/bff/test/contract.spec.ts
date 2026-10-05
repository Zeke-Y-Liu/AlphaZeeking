import axios from 'axios';

import {
  BACKTEST_ALLOWED_FIELDS,
  CREATE_PORTFOLIO_ALLOWED_FIELDS,
  CREATE_PORTFOLIO_POSITION_FIELDS,
  REBALANCE_FREQUENCIES,
  backtestSchema,
  createPortfolioSchema,
  positionSchema,
} from '../src/dto/contracts';
import { INTERNAL_FIELDS } from '../src/transform/internal-fields';

/**
 * 契约快照测试（T5.3）。
 * 以实施书 §6.2 冻结字段集为准，静态断言 BFF 白名单，防止 core schema 漂移。
 * 另附「core OpenAPI 对照」用例：core 可达时自动比对，不可达则跳过（本地 CI 常态）。
 */
describe('契约快照（T5.3，§6.2 冻结字段集）', () => {
  it('POST /api/v1/portfolios 白名单字段冻结', () => {
    expect([...CREATE_PORTFOLIO_ALLOWED_FIELDS]).toEqual([
      'name',
      'pid',
      'note',
      'equal_weight',
      'positions',
    ]);
    expect(Object.keys(createPortfolioSchema)).toEqual([...CREATE_PORTFOLIO_ALLOWED_FIELDS]);
  });

  it('positions[] 白名单字段冻结', () => {
    expect([...CREATE_PORTFOLIO_POSITION_FIELDS]).toEqual(['secid', 'weight']);
    expect(Object.keys(positionSchema)).toEqual([...CREATE_PORTFOLIO_POSITION_FIELDS]);
  });

  it('POST /api/v1/portfolios/:id/backtest 白名单字段冻结', () => {
    expect([...BACKTEST_ALLOWED_FIELDS]).toEqual([
      'start',
      'end',
      'rf',
      'rebalance',
      'benchmark_secid',
    ]);
    expect(Object.keys(backtestSchema)).toEqual([...BACKTEST_ALLOWED_FIELDS]);
  });

  it('backtest rebalance 枚举冻结', () => {
    expect([...REBALANCE_FREQUENCIES]).toEqual([
      'daily',
      'monthly',
      'quarterly',
      'yearly',
      'buyhold',
    ]);
  });

  it('内部字段剔除清单冻结', () => {
    expect([...INTERNAL_FIELDS]).toEqual([
      'id',
      'portfolio_id',
      'run_id',
      'plan_id',
      'artifact_keys',
      'strategy',
      'password_hash',
      'totp_secret',
      '_sa_instance_state',
    ]);
  });
});

interface OpenApiDoc {
  paths?: Record<string, Record<string, unknown>>;
  components?: { schemas?: Record<string, unknown> };
}

/** 尝试拉取 core OpenAPI；不可达返回 null（触发跳过）。 */
async function fetchCoreOpenApi(): Promise<OpenApiDoc | null> {
  const url = process.env.CORE_OPENAPI_URL ?? 'http://127.0.0.1:8082/openapi.json';
  try {
    const res = await axios.get<OpenApiDoc>(url, { timeout: 800 });
    return typeof res.data === 'object' ? res.data : null;
  } catch {
    return null;
  }
}

/** 解析 requestBody schema 的 properties 字段名（best-effort，支持 $ref）。 */
function requestProperties(doc: OpenApiDoc, path: string): string[] | null {
  const op = doc.paths?.[path]?.post as
    | { requestBody?: { content?: Record<string, { schema?: unknown }> } }
    | undefined;
  const schema = op?.requestBody?.content?.['application/json']?.schema as
    | { $ref?: string; properties?: Record<string, unknown> }
    | undefined;
  if (!schema) return null;
  if (schema.properties) return Object.keys(schema.properties);
  if (schema.$ref) {
    const name = schema.$ref.split('/').pop() ?? '';
    const resolved = doc.components?.schemas?.[name] as
      | { properties?: Record<string, unknown> }
      | undefined;
    if (resolved?.properties) return Object.keys(resolved.properties);
  }
  return null;
}

describe('契约快照 · core OpenAPI 对照（core 不可达则跳过）', () => {
  it('core OpenAPI 可达时：BFF 白名单为 core 请求体的子集', async () => {
    const doc = await fetchCoreOpenApi();
    if (!doc) {
      // core 未启动（本地 CI 常态）：跳过对照，静态快照已覆盖契约
      return;
    }

    const cells: Array<[string, readonly string[]]> = [
      ['/api/v1/portfolios', CREATE_PORTFOLIO_ALLOWED_FIELDS],
      ['/api/v1/portfolios/{id}/backtest', BACKTEST_ALLOWED_FIELDS],
    ];
    for (const [path, fields] of cells) {
      const coreProps = requestProperties(doc, path);
      if (!coreProps) continue; // core 尚未实现该端点：跳过
      for (const field of fields) {
        expect(coreProps).toContain(field);
      }
    }
  });
});
