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
    /** 引擎C 冻结五键（T-C6 契约）；缺失/旧 run → 前端按 unavailable 降级呈现。 */
    macro_liquidity?: MacroLiquiditySummary | null;
    /** 引擎D 指数趋势摘要（2026-10-26 契约）；缺失/旧 run → 前端按 unavailable 降级呈现。 */
    index_trend?: IndexTrendSummary | null;
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

/** 数据风险提示（评估不阻断门禁：非全绿时随响应 / runs.summary 下发）。 */
export interface DataRisk {
  /** fail：六项验证有未通过项；unknown：尚未完成同步验证 */
  status: 'fail' | 'unknown';
  message: string;
  failed: { check: string; text?: string | null }[];
  job_id?: string | null;
}

/** 引擎C 宏观流动性摘要（runs.summary.macro_liquidity，字段冻结）。 */
export interface MacroLiquiditySummary {
  status?: 'unavailable';
  error?: string;
  /** 市场档位（-2~+2，正=宽松）：{"A":1,"HK":-1,"US":0}；unavailable 市场不出现 */
  markets?: Record<string, number>;
  global?: number | null;
  global_score?: number | null;
  state?: 'neutral' | 'tight_stress' | 'easy_froth' | null;
  as_of?: string | null;
}

/** 引擎D 单指数摘要条目（runs.summary.index_trend.indexes[id]，2026-10-26 字段冻结）。 */
export interface IndexTrendEntry {
  /** 位置档：high_zone(≥80%) / mid_zone / low_zone(≤20%)；近252根高低区间百分位 */
  position?: 'high_zone' | 'mid_zone' | 'low_zone' | null;
  /** 位置百分位 0-100 */
  pos_q?: number | null;
  /** 趋势三态：up（MA20上方且20日收益>0）/ down / range */
  trend?: 'up' | 'down' | 'range' | null;
  ret_20d?: number | null;
  close?: number | null;
  name?: string | null;
  market?: string | null;
}

/** 引擎D 指数趋势摘要（runs.summary.index_trend，services.review._index_trend_summary 产出；
 * 市场级条件变量，不参与权重决策——纪律同引擎C）。 */
export interface IndexTrendSummary {
  status?: 'unavailable';
  error?: string;
  indexes?: Record<string, IndexTrendEntry>;
  as_of?: string | null;
}

/* ── 引擎板块（指标注册表 v2：因子增删改查 + 数据集联动） ── */

/** 单条引擎指标声明（alpha/beta/macro/index_trend 四引擎同构）。 */
export interface EngineMetric {
  id: string;
  engine: 'alpha' | 'beta' | 'macro' | 'index_trend' | string;
  formula: string;
  dim: string | null;
  market: string | null;
  dataset: string | null;
  scorer: Record<string, unknown> | null;
  params: Record<string, unknown>;
  status: 'active' | 'inactive' | 'experimental' | string;
  version: number;
  hypothesis_id: string | null;
  note: string;
}

/** GET /v1/engines/metrics 响应。 */
export interface EngineMetricsResponse {
  metrics: EngineMetric[];
  changelog_n: number;
}

/** 三引擎自描述（GET /v1/engines/describe）。 */
export interface EngineDescribe {
  id: string;
  name: string;
  kind: string;
  metric_count: number;
  active_count: number;
  dim_weights?: Record<string, number> | null;
}

/** 指标引用数据集的落盘状态（GET /v1/engines/datasets；数据板块卡片共用）。 */
export interface EngineDataset {
  dataset: string;
  synced: boolean;
  as_of: string | null;
  series: number;
  metrics: string[];
  /** 未同步时附引导提示 */
  hint?: string;
}

export interface EngineDatasetsResponse {
  datasets: EngineDataset[];
  missing: number;
}

/** 新增/修改指标时数据集缺失提示（不阻断，引导前往数据页同步）。 */
export interface MetricDataMissing {
  dataset: string;
  synced: boolean;
  as_of: null;
  series: number;
  hint: string;
}

/** POST /v1/engines/metrics 响应（data_missing 仅在声明数据集未落盘时出现）。 */
export interface MetricAddResponse {
  ok: boolean;
  metric: EngineMetric;
  warnings?: string[];
  data_missing?: MetricDataMissing[];
}

/** PATCH /v1/engines/metrics/{mid} 响应。 */
export interface MetricUpdateResponse {
  ok: boolean;
  before: EngineMetric;
  after: EngineMetric;
  data_missing?: MetricDataMissing[];
}

/** DELETE（软删）响应。 */
export interface MetricRemoveResponse {
  ok: boolean;
  metric: EngineMetric;
  warnings?: string[];
}

/** POST /v1/engines/metrics 入参（spec 与门禁字段）。 */
export interface EngineMetricAddRequest {
  engine: string;
  spec: {
    id: string;
    formula: string;
    dim?: string | null;
    market?: string | null;
    dataset?: string | null;
    scorer?: Record<string, unknown> | null;
    params?: Record<string, unknown>;
    status?: string;
    note?: string;
  };
  hypothesis_id: string;
  reason?: string;
}

/** PATCH /v1/engines/metrics/{mid} 入参。 */
export interface EngineMetricUpdateRequest {
  patch: Record<string, unknown>;
  hypothesis_id?: string | null;
  reason?: string;
  engine?: string | null;
}

/** 异步 Job 提交响应（§6.1）。review 端点额外携带 data_risk（全绿为 null）。 */
export interface JobAccepted {
  job_id: string;
  poll: string;
  data_risk?: DataRisk | null;
}

/* ── 单公司分析（M10） ─────────────────────────────── */
/** 分析档案；`shadow_pid` 为单标的 100% 影子组合（供回测/评估复用）。 */
export interface AnalysisProfile {
  id: number;
  aid: string;
  secid: string;
  name: string;
  note?: string | null;
  modules: Record<string, boolean>;
  status: PortfolioStatus;
  shadow_pid: string;
  created_at: string;
  updated_at: string;
  latest_run?: RunSummary | null;
}

/** 个股新闻条目（东财；dt 为 "YYYY-MM-DD HH:MM:SS"）。 */
export interface AnalysisNewsItem {
  dt?: string | null;
  title: string;
  content?: string;
  source?: string;
  url?: string;
}

/** GET /analysis/{aid}/news 响应；无数据时 available=false + note。 */
export interface AnalysisNews {
  secid: string;
  available: boolean;
  count?: number;
  note?: string;
  items: AnalysisNewsItem[];
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

/* ── 引擎C 宏观流动性（T-C6 冻结契约 + T-C7 前端） ───────────── */

/** 引擎C 冻结五键（core ``runs.summary.macro_liquidity``，services.review._macro_summary
 * 产出；API.md §4.4 注释同步：字段级冻结，向后兼容只增不改）。 */
export interface MacroLiquiditySummary {
  /** 全局档位：-2…+2；unavailable 形态为 'unavailable'。 */
  state: string;
  /** 全局档位（数值）；降级为 'unavailable'。 */
  global: number | string | null;
  /** 三市场档位；降级态三市场均为 'unavailable'。 */
  markets: { A: number | string | null; HK: number | string | null; US: number | string | null };
  /** 全局打分（维度等权聚合）；降级为 null。 */
  global_score: number | null;
  /** 档位基准日（与评估同日）；降级为 null。 */
  as_of: string | null;
}

/** 引擎C regime 块（applib review.json 全量产物，T-C2/T-C4：冻结五键 +
 *  status/detail/notes 扩展键；summary 层由 core 提炼为 MacroLiquiditySummary）。 */
export interface MacroLiquidityBlock extends MacroLiquiditySummary {
  status?: string | null;
  reason?: string | null;
  detail?: Record<string, unknown> | null;
  notes?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/* ── 引擎D 指数趋势（2026-10-26：位置百分位 + 均线趋势，市场级条件变量）── */

/** 引擎D 单指数全量条目（applib review.json → index_trend.indexes[id]）：
 * 位置族（pos_q/dist_high/dist_low/position）+ 趋势族（ret_20d/ret_60d/ma_fast/ma_slow/
 * trend）+ 降级族（status: insufficient/unavailable/inactive）。 */
export interface IndexTrendDetail extends IndexTrendEntry {
  status?: string | null;
  as_of?: string | null;
  bars_n?: number | null;
  dist_high?: number | null;
  dist_low?: number | null;
  pos_high_252?: number | null;
  pos_low_252?: number | null;
  ret_60d?: number | null;
  ma_fast?: number | null;
  ma_slow?: number | null;
  ma_fast_above?: boolean | null;
  ma_fast_above_slow?: boolean | null;
  source?: string | null;
  note?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

/** 引擎D 指数趋势块（applib review.json 全量产物；summary 层由 core 提炼为 IndexTrendSummary）。 */
export interface IndexTrendBlock {
  status?: string | null;
  error?: string | null;
  as_of?: string | null;
  indexes?: Record<string, IndexTrendDetail>;
  [key: string]: unknown;
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
  /** 引擎C regime 块（T-C2/T-C4 全量产物；前端卡片消费 runs.summary 五键，此为报告层扩展）。 */
  macro_liquidity?: MacroLiquidityBlock | null;
  /** 引擎D 指数趋势块（2026-10-26 全量产物；前端卡片消费 runs.summary.index_trend 摘要）。 */
  index_trend?: IndexTrendBlock | null;
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
