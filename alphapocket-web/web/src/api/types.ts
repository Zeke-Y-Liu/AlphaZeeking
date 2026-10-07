/**
 * API 契约类型（手写，与实施书 §6 端点清单 / §5.1 DDL 对齐）。
 *
 * ⚠️ 待替换：core 的 OpenAPI（T1.1 并行交付）冻结后，本文件将由自动生成替代，
 * 见 T5.3 / T7.2；执行 `pnpm gen:api` 生成 src/api/generated/schema.d.ts 后逐步迁移。
 * 在此之前，请勿依赖未在本文件声明的字段。
 */

/* ── 枚举（§5.1 / §6.3） ─────────────────────────── */
export type AssetType = 'STOCK' | 'ETF' | 'LOF';
/** V4-β 市场：A=A股（股票/ETF/LOF） HK=港股 US=美股。 */
export type Market = 'A' | 'HK' | 'US';
export type PortfolioStatus = 'active' | 'archived';
export type JobType = 'data_sync' | 'review' | 'backtest' | 'rebalance_plan' | 'exec_orders';
export type JobStatus = 'queued' | 'running' | 'done' | 'failed';
export type RunType = JobType;
export type PlanStatus = 'draft' | 'superseded' | 'executed' | 'cancelled';
export type ExecOrderStatus = 'draft' | 'confirmed' | 'executed' | 'cancelled';
export type SyncMode = 'bootstrap' | 'incremental' | 'refresh';
export type VerifyStatus = 'pass' | 'fail';

/* ── 领域对象 ──────────────────────────────────── */
export interface Security {
  secid: string;
  code: string;
  name: string;
  market: Market | string;
  asset_type: AssetType;
  industry?: string | null;
  sina_symbol: string;
  ftype?: string | null;
  settle?: string | null;
  cost?: string | null;
  /** V4-β：交易计价币种 CNY/HKD/USD（A股缺省 CNY）。 */
  currency?: string | null;
  /** V4-β：港股通标记（仅港股有值：true=港股通标的 / false=非港股通 / null=A股或美股）。 */
  hk_connect?: boolean | null;
  source: string;
  created_at: string;
}

export interface Position {
  secid: string;
  weight: number; // 百分数，如 20.0
  name?: string;
  market?: Market | string | null;
  asset_type?: AssetType;
  currency?: string | null;
  hk_connect?: boolean | null;
  settle?: string;
  cost?: string;
}

/* ── V4-β 实时行情 ─────────────────────────────── */
/** 单标的实时行情（GET /v1/market/quotes）。 */
export interface Quote {
  secid: string;
  name: string;
  market: Market | string | null;
  market_label?: string;
  currency?: string | null;
  price?: number | null;
  /** 人民币折算价（A股=原值；港/美按汇率折算）。 */
  price_cny?: number | null;
  prev_close?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  chg?: number | null;
  pct?: number | null;
  volume?: number | null;
  amount?: number | null;
  time?: string | null;
  source?: string;
  hk_connect?: boolean | null;
  asset_type?: AssetType | null;
  /** 双源均失败时带 error，不影响其余标的。 */
  error?: string;
}

/** 汇率快照（随行情响应返回）。 */
export interface FxSnapshot {
  HKD_CNY?: number | null;
  USD_CNY?: number | null;
  as_of?: string | null;
  time?: string | null;
  source?: string;
  error?: string;
}

export interface QuotesResponse {
  quotes: Quote[];
  fx: FxSnapshot;
  cached?: boolean;
}

/** 组合最近一次 run 摘要（core ``portfolios.latest_run`` 原样返回：
 * ``{run_uid, type, as_of, created_at, summary}``；四指标位于 ``summary.portfolio_metrics``）。 */
export interface RunSummary {
  run_uid: string;
  type: RunType;
  as_of?: string | null;
  created_at?: string | null;
  summary?: {
    as_of?: string | null;
    portfolio_metrics?: ReviewMetrics | null;
    position_count?: number;
    signal_count?: number;
    signal_states?: unknown;
  } | null;
}

export interface Portfolio {
  id: number;
  pid: string;
  name: string;
  note?: string | null;
  base_ccy: string;
  strategy: Record<string, unknown>;
  status: PortfolioStatus;
  created_at: string;
  updated_at: string;
  positions?: Position[];
  position_count?: number;
  latest_run?: RunSummary | null;
}

export interface Job {
  id: string;
  type: JobType;
  portfolio_id?: number | null;
  status: JobStatus;
  params: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string | null;
  progress: number; // 0-100
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
}

export interface Run {
  id: number;
  run_uid: string;
  job_id: string;
  portfolio_id?: number | null;
  type: RunType;
  as_of?: string | null;
  summary?: Record<string, unknown> | null;
  artifact_keys: string[];
  created_at: string;
}

/** 验证 detail 冻结结构（services.verification._parse_detail）。 */
export interface VerifyDetail {
  text: string;
  count?: number | null;
  exempt?: number | null;
  max_diff?: number | null;
  sources?: number | null;
  error?: string | null;
}

export interface VerifyResult {
  id?: number;
  job_id: string;
  check: string; // pit_audit | golden_test | cross_source | calendar_check | adjust_check | provenance_check
  status: VerifyStatus;
  detail: VerifyDetail;
  created_at: string;
}

/** 六项验证汇总（GET /data/status → verification）。 */
export interface VerifySummary {
  status: 'pass' | 'fail' | 'unknown';
  job_id: string | null;
  created_at: string | null;
  checks: { check: string; status: VerifyStatus; detail: VerifyDetail }[];
  failed: { check: string; text?: string | null }[];
}

/* ── 调仓 plan / 执行单契约（T4.1–T4.4，字段冻结） ──────── */

/** plan payload.diff_trades[]（applib tools.portfolio_rebalance_plan）。 */
export interface PlanDiffTrade {
  side: 'evaluate_sell' | 'hold_confirm' | string;
  secid: string;
  name: string;
  from: number;
  to: number;
  reason: string;
  evidence: string;
  settle: string;
  cost: string;
}

/** `RebalancePlan.payload` 的结构（`GET /plans/{id}`）。 */
export interface RebalancePlanPayload {
  plan_id: string;
  portfolio: string;
  generated_by?: string;
  as_of: string;
  diff_trades: PlanDiffTrade[];
  turnover_est: string;
  cost_est_cny: number;
  cost_note?: string;
  status: string;
}

export interface RebalancePlan {
  id: number;
  plan_uid: string;
  portfolio_id: number;
  run_id?: number | null;
  status: PlanStatus;
  payload: RebalancePlanPayload;
  created_at: string;
}

/** 执行单列表项（`GET /portfolios/{pid}/plans` 返回的摘要 / `.../plans` 无 payload）。 */
export interface RebalancePlanSummary {
  id: number;
  plan_uid: string;
  portfolio_id: number;
  run_id?: number | null;
  status: PlanStatus;
  cost_est_cny?: number | null;
  created_at: string;
}

/** 委托行（applib exec_orders.build_exec_orders 的 orders[]）。 */
export interface ExecOrderRow {
  instruction: string;
  secid: string;
  name: string;
  asset_type: AssetType;
  direction: string;
  cur_weight: number;
  tgt_weight: number;
  delta_weight: number;
  price: number;
  cur_shares: number;
  tgt_shares: number;
  order_shares: number;
  lots: number;
  order_amount: number;
  fee: number;
  limit_ref: number;
  band_low: number;
  band_high: number;
  state: 'above' | 'below' | 'neutral' | string;
  settle: string;
  reason: string;
  evidence: string;
}

export interface ExecOrderSheet {
  exec_id: string;
  portfolio: string;
  portfolio_name: string;
  plan_id: string;
  as_of: string;
  next_trade_date: string;
  holiday_days: number;
  capital_cny: number;
  base_ccy: string;
  generated_at: string;
  status: string;
  discipline: string;
  orders: ExecOrderRow[];
  actionable: ExecOrderRow[];
  cash: {
    sell_proceeds: number;
    buy_outlay: number;
    net_cash: number;
    end_invested_cny: number;
    end_invested_pct: number;
    end_cash_cny: number;
    end_cash_pct: number;
  };
  fees: { commission_rate: string; min_fee_cny: number; total_fee_cny: number; note: string };
  summary: {
    n_orders: number;
    n_sell: number;
    n_buy: number;
    turnover_cny: number;
    turnover_pct: number;
  };
  checks: { item: string; detail: string; status: string }[];
  risks: string[];
}

/** 成交登记入参（POST .../fill）。 */
export interface ExecOrderFill {
  secid: string;
  price: number;
  shares: number;
}

/** 已登记成交（exec.fills）。 */
export interface ExecOrderFills {
  items: {
    secid: string;
    name: string;
    direction: string;
    price: number;
    shares: number;
    order_shares: number;
    amount: number;
  }[];
  n: number;
  total_amount: number;
  partial: boolean;
}

/** 状态时间线条目（audit_log → exec_order）。 */
export interface ExecOrderTimelineItem {
  at?: string | null;
  action: string;
  actor: string;
  payload: Record<string, unknown>;
}

/** 执行单详情（POST /plans/{id}/exec-orders 与 GET /exec-orders/{id} 同构）。 */
export interface ExecOrder {
  id: number;
  exec_uid: string;
  plan_id: number;
  plan_uid?: string | null;
  portfolio_id: number;
  capital_cny: number;
  status: ExecOrderStatus;
  created_at: string;
  confirmed_at?: string | null;
  confirmed_by?: string | null;
  filled_at?: string | null;
  fills?: ExecOrderFills | null;
  sheet: ExecOrderSheet;
  timeline: ExecOrderTimelineItem[];
}

/** 执行单列表项（GET /portfolios/{pid}/exec-orders，不含大 sheet）。 */
export interface ExecOrderSummary {
  id: number;
  exec_uid: string;
  plan_id: number;
  portfolio_id: number;
  capital_cny: number;
  status: ExecOrderStatus;
  created_at: string;
  confirmed_at?: string | null;
  filled_at?: string | null;
}

/** GET /data/status（T1.5，services.data_status.build_status）。 */
export interface DataStatusSecurity {
  secid: string;
  code: string;
  name: string;
  market: Market | string;
  asset_type: AssetType;
  currency?: string | null;
  hk_connect?: boolean | null;
  last_bar: string | null;
  lag_days: number | null;
  missing: number | null;
  has_bars: boolean;
}

export interface DataStatus {
  as_of: string | null;
  /** V4-β：分市场最新 bar 日期（三市场交易日历独立）。 */
  as_of_by_market?: Record<string, string | null>;
  universe: {
    count: number;
    types: Record<AssetType, number>;
    markets?: Record<string, number>;
  };
  securities: DataStatusSecurity[];
  verification: VerifySummary;
  latest_sync: {
    job_id: string;
    status: JobStatus;
    progress: number;
    finished_at: string | null;
  } | null;
}

/** 异步 Job 提交响应（§6.1）。 */
export interface JobAccepted {
  job_id: string;
  poll: string;
}

/* ── 认证（T5.2 · BFF 契约，冻结） ─────────────────── */
export interface AuthUser {
  id: number;
  username: string;
  role: string;
}

/** POST /auth/login、POST /auth/refresh 的响应体（token 走响应体，前端持久化见 auth store 的 XSS 说明）。 */
export interface AuthTokens {
  access: string;
  refresh: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
}

/* ── 评估产物契约（applib review.json，冻结；方案书 §5.2 / T6.4） ── */

/** 体检指标（review.json → portfolio_metrics）。 */
export interface ReviewMetrics {
  day_ret?: number | null;
  ret_20d?: number | null;
  ret_ytd?: number | null;
  ret_1y?: number | null;
  max_dd_1y?: number | null;
  nav?: number | null;
  method?: string | null;
}

/** 单只持仓（review.json → positions[]）。基金/指数类无财报 → alpha 估值/质量/成长字段为 null。 */
export interface ReviewPosition {
  secid: string;
  name?: string | null;
  industry?: string | null;
  weight?: number | null;
  asset_type?: AssetType | null;
  settle?: string | null;
  cost?: string | null;
  close?: number | null;
  day_pct?: number | null;
  alpha?: {
    val_pe_ttm?: number | null;
    val_pb?: number | null;
    val_pe_pct?: number | null;
    val_pb_pct?: number | null;
    div_yield_ttm?: number | null;
    qua_roe_ttm?: number | null;
    qua_margin?: number | null;
    grow_rev_yoy?: number | null;
    grow_np_yoy?: number | null;
    mom_12_1?: number | null;
    vol_250?: number | null;
    used_period?: string | null;
    pe_hist_n?: number | null;
  } | null;
  beta?: {
    mom_20d?: number | null;
    vol_20d?: number | null;
    ma20?: number | null;
    ma20_break?: boolean | null;
    state?: 'above' | 'below' | 'neutral' | null;
    high20_touch?: boolean | null;
    dist_high20?: number | null;
    high20_intra?: number | null;
    low20?: number | null;
    sigma_daily?: number | null;
    band_low?: number | null;
    band_high?: number | null;
  } | null;
  dims?: Record<string, unknown> | null;
  grades?: Record<string, unknown> | null;
  composite?: number | null;
  composite_grade?: string | null;
  flags?: string[] | null;
}

/** 引擎B 信号（review.json → signals[]）。 */
export interface ReviewSignal {
  secid: string;
  name?: string | null;
  signal_id?: string | null;
  type?: string | null;
  detail?: string | null;
  evidence?: string | null;
  action?: string | null;
  cooldown?: boolean | number | string | null;
}

/** review.json 产物根对象（report/view 概览数据源）。 */
export interface ReviewPayload {
  id?: number | string;
  name?: string | null;
  as_of?: string | null;
  base_ccy?: string | null;
  note?: string | null;
  next_trade_date?: string | null;
  holiday_days?: number | null;
  portfolio_metrics?: ReviewMetrics | null;
  positions?: ReviewPosition[];
  signals?: ReviewSignal[];
  factor_health?: { active?: string[]; weakening?: string[] } | null;
  data_quality?: {
    last_bar?: string | null;
    calendar_next?: string | null;
    sources?: unknown;
    provenance?: unknown;
  } | null;
}

/* ── 请求体 ────────────────────────────────────── */
export interface CreatePortfolioRequest {
  name: string;
  pid?: string;
  note?: string;
  positions: { secid: string; weight: number }[];
  equal_weight?: boolean;
}

export interface BacktestRequest {
  start?: string;
  end?: string;
  rf?: number;
  rebalance?: string;
  benchmark_secid?: string;
  cost_bps?: number;
}

export interface DataSyncRequest {
  mode: SyncMode;
  secid?: string;
}

/* ── 回测产物契约（applib backtest.json，冻结；方案书 §9.7 / T6.5） ── */

/** 回测指标（applib backtest strategy_metrics / benchmark.metrics）。 */
export interface BacktestMetrics {
  total_return: number | null;
  cagr: number | null;
  ann_vol: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_dd: number | null;
  calmar: number | null;
  win_rate: number | null;
  dd_peak?: string | null;
  dd_trough?: string | null;
  dd_recover?: string | null;
  dd_days?: number | null;
  n_days?: number | null;
}

export interface BacktestBenchmark {
  secid: string;
  name: string;
  metrics: BacktestMetrics;
  series?: [string, number][];
}

export interface BacktestPerAsset {
  secid: string;
  name: string;
  weight: number;
  total_return: number | null;
  cagr: number | null;
  ann_vol: number | null;
  max_dd: number | null;
  corr: number | null;
}

/** backtest.json 产物完整结构（downgraded 前端可直接读）。 */
export interface BacktestPayload {
  portfolio: string;
  portfolio_name: string;
  strategy: string;
  rebalance: string;
  benchmark_secid: string;
  start: string;
  end: string;
  n_days: number;
  n_rebalances: number;
  cost_bps: number;
  rf: number;
  series: [string, number][];
  benchmark: BacktestBenchmark | null;
  strategy_metrics: BacktestMetrics;
  excess: { total: number | null; cagr: number | null };
  monthly: Record<string, number>;
  yearly: Record<string, Record<string, number>>;
  per_asset: BacktestPerAsset[];
  cost: { n: number; total: number; avg_turnover: number };
  data_quality?: {
    missing_bars: Record<string, number>;
    note?: string;
    corporate_action?: string;
  } | null;
}

/* ── 错误（§6.3 错误体 {code,message,detail}） ───────── */
export interface ApiErrorBody {
  code: string;
  message: string;
  detail?: unknown;
}

/** 统一 API 错误：由 client 的错误拦截器解包后端 {code,message,detail}。 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly detail?: unknown;

  constructor(code: string, message: string, status = 0, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}
