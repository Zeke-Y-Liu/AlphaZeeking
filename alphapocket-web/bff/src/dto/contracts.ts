/**
 * 核心写端点的 DTO 白名单（T5.3）。
 *
 * 字段集冻结自实施书 §6.2 端点清单；此处以常量形式显式声明，供：
 * 1) 校验器构造 schema；
 * 2) 契约快照测试断言（防 core schema 漂移）。
 *
 * 注意：这里只做「类型 / 枚举 / 必填」校验与多余字段剥离；
 * 业务语义（标的存在、权重和 ∈ [99.5,100.5] 等）仍由 core 负责（§6.2）。
 */
import type { ObjectSchema } from './validation';

/** 回测再平衡频率枚举（§6.2：rebalance?）。 */
export const REBALANCE_FREQUENCIES = [
  'daily',
  'monthly',
  'quarterly',
  'yearly',
  'buyhold',
] as const;

export type RebalanceFrequency = (typeof REBALANCE_FREQUENCIES)[number];

/** POST /api/v1/portfolios 白名单字段（§6.2）。 */
export const CREATE_PORTFOLIO_ALLOWED_FIELDS = [
  'name',
  'pid',
  'note',
  'equal_weight',
  'positions',
] as const;

/** positions[] 元素白名单字段（§6.2）。 */
export const CREATE_PORTFOLIO_POSITION_FIELDS = ['secid', 'weight'] as const;

/** POST /api/v1/portfolios/:id/backtest 白名单字段（§6.2）。 */
export const BACKTEST_ALLOWED_FIELDS = [
  'start',
  'end',
  'rf',
  'rebalance',
  'benchmark_secid',
] as const;

/** positions[] 元素 schema。 */
export const positionSchema: ObjectSchema = {
  secid: { kind: 'string', required: true, nonEmpty: true },
  weight: { kind: 'number', required: true },
};

/** POST /api/v1/portfolios 请求体 schema。 */
export const createPortfolioSchema: ObjectSchema = {
  name: { kind: 'string', required: true, nonEmpty: true },
  pid: { kind: 'string' },
  note: { kind: 'string' },
  equal_weight: { kind: 'boolean' },
  positions: { kind: 'array', required: true, item: positionSchema },
};

/** POST /api/v1/portfolios/:id/backtest 请求体 schema。 */
export const backtestSchema: ObjectSchema = {
  start: { kind: 'string' },
  end: { kind: 'string' },
  rf: { kind: 'number' },
  rebalance: { kind: 'enum', values: REBALANCE_FREQUENCIES },
  benchmark_secid: { kind: 'string' },
};

/** 需要 DTO 校验的写端点（method + 路径表达式 + schema）。 */
export const DTO_ROUTES: ReadonlyArray<{
  method: string;
  pattern: string;
  schema: ObjectSchema;
}> = [
  { method: 'POST', pattern: '/api/v1/portfolios', schema: createPortfolioSchema },
  {
    method: 'POST',
    pattern: '/api/v1/portfolios/:id/backtest',
    schema: backtestSchema,
  },
];
