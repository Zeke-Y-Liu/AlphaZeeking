/**
 * T7.1 E2E 确定性夹具（前端 E2E + 夹具，不走真实后端/外网）。
 *
 * 通过 Playwright `page.route('**\/api/**')` 拦截前端全部 API 调用，返回冻结的
 * α 锚点快照数据（§1.2），保证可重复：
 *  - 剧本 A：新建 multi5（新 workspace）→ 验证全绿 → 评估 → 回测（A2 锚点）→ 调仓 → 执行单 → 确认 → 登记成交；
 *  - 剧本 B：seed cn4 回归（评估 + 回测）；
 *  - 分支：门禁 409 VERIFY_GATE_FAILED、互斥 409 JOB_LOCKED、登录 429 ACCOUNT_LOCKED。
 *
 * 关键：mock 严格复刻真实契约（如 `/v1/runs` 的 portfolio_id 必须是数值主键，
 * 传 'NaN' 会 422），因此前端若用 Number(pid) 过滤会被 E2E 直接暴露。
 */
import type { Page, Route } from '@playwright/test';

export type Scenario = 'multi5' | 'cn4';

const AS_OF = '2026-09-30';
const SYNCED_AS_OF = '2026-10-05';
const NEXT_TRADE_DATE = '2026-10-08';
const CREATED_AT = '2026-09-30T15:00:00Z';

/* ── 标的（逐字段对齐 core/app/seed.py） ─────────────────────── */

const STOCK_SETTLE = 'A股 T+1';
const STOCK_COST = '印花税卖出0.05%+佣金';
const ETF_COST = 'ETF 免印花税·佣金约万2.5';
const LOF_COST = 'LOF 免印花税·佣金约万2.5';

interface SecFixture {
  secid: string;
  code: string;
  name: string;
  asset_type: 'STOCK' | 'ETF' | 'LOF';
  industry: string;
  sina_symbol: string;
  ftype: string | null;
  settle: string;
  cost: string;
}

const CN4: SecFixture[] = [
  { secid: '601058.SH', code: '601058', name: '赛轮集团', asset_type: 'STOCK', industry: '汽车零部件·轮胎', sina_symbol: 'sh601058', ftype: null, settle: STOCK_SETTLE, cost: STOCK_COST },
  { secid: '002014.SZ', code: '002014', name: '永新股份', asset_type: 'STOCK', industry: '塑料软包装', sina_symbol: 'sz002014', ftype: null, settle: STOCK_SETTLE, cost: STOCK_COST },
  { secid: '600660.SH', code: '600660', name: '福耀玻璃', asset_type: 'STOCK', industry: '汽车零部件·汽车玻璃', sina_symbol: 'sh600660', ftype: null, settle: STOCK_SETTLE, cost: STOCK_COST },
  { secid: '002027.SZ', code: '002027', name: '分众传媒', asset_type: 'STOCK', industry: '广告传媒', sina_symbol: 'sz002027', ftype: null, settle: STOCK_SETTLE, cost: STOCK_COST },
];

const MULTI5: SecFixture[] = [
  { secid: '510300.SH', code: '510300', name: '沪深300ETF', asset_type: 'ETF', industry: 'A股宽基指数', sina_symbol: 'sh510300', ftype: 'etf', settle: 'T+1', cost: ETF_COST },
  { secid: '501018.SH', code: '501018', name: '南方原油LOF', asset_type: 'LOF', industry: '原油(QDII)', sina_symbol: 'sh501018', ftype: 'lof', settle: 'T+0(跨境LOF)', cost: LOF_COST },
  { secid: '518880.SH', code: '518880', name: '黄金ETF', asset_type: 'ETF', industry: '黄金', sina_symbol: 'sh518880', ftype: 'etf', settle: 'T+0', cost: ETF_COST },
  { secid: '513100.SH', code: '513100', name: '纳指ETF', asset_type: 'ETF', industry: '美股指数(QDII-纳指100)', sina_symbol: 'sh513100', ftype: 'etf', settle: 'T+0(跨境)', cost: ETF_COST },
  { secid: '511260.SH', code: '511260', name: '十年国债ETF', asset_type: 'ETF', industry: '国债', sina_symbol: 'sh511260', ftype: 'etf', settle: 'T+0', cost: ETF_COST },
];

const ALL_SECURITIES: SecFixture[] = [...CN4, ...MULTI5];

function securityOut(s: SecFixture) {
  return {
    secid: s.secid,
    code: s.code,
    name: s.name,
    market: 'A',
    asset_type: s.asset_type,
    industry: s.industry,
    sina_symbol: s.sina_symbol,
    ftype: s.ftype,
    settle: s.settle,
    cost: s.cost,
    source: 'seed',
    created_at: CREATED_AT,
  };
}

/* ── 组合 ───────────────────────────────────────────────────── */

interface PositionFixture {
  secid: string;
  weight: number;
  name: string;
  asset_type: string;
  settle?: string;
  cost?: string;
}

interface PortfolioFixture {
  id: number;
  pid: string;
  name: string;
  note: string;
  base_ccy: string;
  strategy: Record<string, unknown>;
  status: string;
  created_at: string;
  updated_at: string;
  positions: PositionFixture[];
}

function makePositions(secs: SecFixture[]): PositionFixture[] {
  const w = Number((100 / secs.length).toFixed(1));
  return secs.map((s) => ({ secid: s.secid, weight: w, name: s.name, asset_type: s.asset_type, settle: s.settle, cost: s.cost }));
}

const CN4_PORTFOLIO: PortfolioFixture = {
  id: 1,
  pid: 'cn4',
  name: 'cn4 · 赛轮/永新/福耀/分众',
  note: '等权名义组合 25%×4',
  base_ccy: 'CNY',
  strategy: {},
  status: 'active',
  created_at: CREATED_AT,
  updated_at: CREATED_AT,
  positions: makePositions(CN4),
};

const MULTI5_PORTFOLIO: PortfolioFixture = {
  id: 2,
  pid: 'multi5',
  name: 'multi5 · 沪深300/南方原油/黄金/纳指/十年国债',
  note: '五资产等权名义组合 20%×5',
  base_ccy: 'CNY',
  strategy: {},
  status: 'active',
  created_at: CREATED_AT,
  updated_at: CREATED_AT,
  positions: makePositions(MULTI5),
};

/* ── 产物构建（α 锚点） ─────────────────────────────────────── */

function buildReview(pf: PortfolioFixture) {
  const n = pf.positions.length;
  const secMap = new Map(ALL_SECURITIES.map((s) => [s.secid, s]));
  return {
    id: pf.pid,
    name: pf.name,
    as_of: AS_OF,
    base_ccy: 'CNY',
    note: pf.note,
    next_trade_date: NEXT_TRADE_DATE,
    holiday_days: 3,
    portfolio_metrics: {
      day_ret: 0.0042,
      ret_20d: 0.031,
      ret_ytd: 0.087,
      ret_1y: 0.142,
      max_dd_1y: -0.096,
      nav: 1.056,
      method: '价格收益（ETF/LOF 未含分红）',
    },
    positions: pf.positions.map((p, i) => {
      const s = secMap.get(p.secid)!;
      return {
        secid: p.secid,
        name: p.name,
        industry: s.industry,
        weight: p.weight,
        asset_type: s.asset_type,
        settle: s.settle,
        cost: s.cost,
        close: Number((3 + i * 1.7).toFixed(3)),
        day_pct: Number(((i % 3) - 1) * 0.004),
        alpha: {
          val_pe_ttm: s.asset_type === 'STOCK' ? 12 + i : null,
          val_pb: s.asset_type === 'STOCK' ? 1.2 + i * 0.1 : null,
          div_yield_ttm: s.asset_type === 'STOCK' ? 0.031 : null,
          qua_roe_ttm: s.asset_type === 'STOCK' ? 0.14 - i * 0.01 : null,
          qua_margin: s.asset_type === 'STOCK' ? 0.32 - i * 0.02 : null,
          grow_rev_yoy: s.asset_type === 'STOCK' ? 0.08 + i * 0.01 : null,
          grow_np_yoy: s.asset_type === 'STOCK' ? 0.11 + i * 0.01 : null,
          mom_12_1: 0.05 + i * 0.01,
          vol_250: 0.18 + i * 0.01,
          used_period: '2026Q2',
        },
        beta: {
          mom_20d: Number((0.02 - i * 0.01).toFixed(4)),
          ma20: Number((3 + i * 1.7).toFixed(3)),
          ma20_break: i === 0,
          state: i === 0 ? 'below' : i === 1 ? 'above' : 'neutral',
          high20_touch: i === 1,
          dist_high20: -0.012,
        },
        composite: 60 + i * 3,
        composite_grade: 'B',
        flags: [],
      };
    }),
    signals: pf.positions.map((p, i) => ({
      secid: p.secid,
      name: p.name,
      signal_id: `SIG-${i + 1}`,
      type: 'engine_b',
      detail: '20 日动量转弱',
      evidence: `mom_20d ${(0.02 - i * 0.01).toFixed(2)}`,
      action: i === 0 ? '卖出预警 · 减仓' : '持有观察',
      cooldown: i === 2,
    })),
    factor_health: { active: ['mom_12_1'], weakening: [] },
    data_quality: { last_bar: AS_OF, calendar_next: NEXT_TRADE_DATE },
  };
}

function buildBacktest(pf: PortfolioFixture) {
  const isMulti = pf.pid.startsWith('multi5');
  const s = isMulti
    ? { total: 1.056, cagr: 0.139, vol: 0.11, sharpe: 1.25, sortino: 1.16, dd: -0.096, calmar: 1.45, win: 0.569 }
    : { total: 0.452, cagr: 0.071, vol: 0.165, sharpe: 0.43, sortino: 0.38, dd: -0.183, calmar: 0.39, win: 0.512 };
  const b = { total: -0.169, cagr: -0.031, vol: 0.18, sharpe: -0.17, sortino: -0.22, dd: -0.451, calmar: -0.29, win: 0.47 };
  const nDays = 1393;
  const nReb = 69;
  const series: [string, number][] = [
    ['2021-01-04', 1.0],
    ['2023-01-03', 1.18],
    ['2026-09-30', Number((1 + s.total).toFixed(4))],
  ];
  const benchSeries: [string, number][] = [
    ['2021-01-04', 1.0],
    ['2023-01-03', 0.86],
    ['2026-09-30', Number((1 + b.total).toFixed(4))],
  ];
  return {
    portfolio: pf.pid,
    portfolio_name: pf.name,
    strategy: '等权 · 月度再平衡',
    rebalance: 'monthly',
    benchmark_secid: '510300.SH',
    start: '2021-01-04',
    end: AS_OF,
    n_days: nDays,
    n_rebalances: nReb,
    cost_bps: 2.5,
    rf: 0,
    series,
    benchmark: {
      secid: '510300.SH',
      name: '沪深300ETF',
      metrics: { total_return: b.total, cagr: b.cagr, ann_vol: b.vol, sharpe: b.sharpe, sortino: b.sortino, max_dd: b.dd, calmar: b.calmar, win_rate: b.win, n_days: nDays },
      series: benchSeries,
    },
    strategy_metrics: { total_return: s.total, cagr: s.cagr, ann_vol: s.vol, sharpe: s.sharpe, sortino: s.sortino, max_dd: s.dd, calmar: s.calmar, win_rate: s.win, n_days: nDays },
    excess: { total: Number((s.total - b.total).toFixed(4)), cagr: Number((s.cagr - b.cagr).toFixed(4)) },
    monthly: { '2026-09': 0.012, '2026-08': -0.004 },
    yearly: {
      '2025': { '01': 0.02, '06': -0.01, '12': 0.03, Y: 0.06 },
      '2026': { '01': 0.01, '09': 0.012, Y: 0.041 },
    },
    per_asset: pf.positions.map((p, i) => ({
      secid: p.secid,
      name: p.name,
      weight: p.weight,
      total_return: Number((s.total - i * 0.08).toFixed(4)),
      cagr: Number((s.cagr - i * 0.01).toFixed(4)),
      ann_vol: Number((s.vol + i * 0.01).toFixed(4)),
      max_dd: Number((s.dd + i * 0.01).toFixed(4)),
      corr: Number((0.72 - i * 0.1).toFixed(2)),
    })),
    cost: { n: nReb, total: 1234.56, avg_turnover: 0.012 },
    data_quality: { missing_bars: {}, note: '价格收益口径（ETF/LOF 未含分红）', corporate_action: '份额折算已登记' },
  };
}

function buildPlan(pf: PortfolioFixture) {
  const planUid = pf.pid.startsWith('multi5') ? 'RP-20260930-MULTI5' : 'RP-20260930-CN4';
  return {
    id: pf.id * 100 + 1,
    plan_uid: planUid,
    portfolio_id: pf.id,
    run_id: null,
    status: 'draft',
    payload: {
      plan_id: planUid,
      portfolio: pf.pid,
      generated_by: 'engine_b',
      as_of: AS_OF,
      diff_trades: [
        { side: 'evaluate_sell', secid: '510300.SH', name: '沪深300ETF', from: 20.0, to: 15.0, reason: '引擎B：跌破 MA20，触发减仓', evidence: 'mom_20d -3.2%', settle: 'T+1', cost: 'ETF 免印花税' },
        { side: 'evaluate_sell', secid: '518880.SH', name: '黄金ETF', from: 20.0, to: 15.0, reason: '引擎B：触及20日高后回落', evidence: 'high20_touch true', settle: 'T+0', cost: 'ETF 免印花税' },
      ],
      turnover_est: '10.0%',
      cost_est_cny: 40.01,
      cost_note: '佣金万2.5、最低¥5；ETF 免印花税（C2 修复后口径）',
      status: 'draft',
    },
    created_at: CREATED_AT,
  };
}

/** 执行单 sheet（对齐 A4 锚点：2 笔卖出 / 回款 ¥160,063 / 费用 ¥40.01 / 期末现金 15.8%）。 */
function buildExecOrder(pf: PortfolioFixture, plan: ReturnType<typeof buildPlan>, capital: number) {
  const mk = (over: Record<string, unknown>) => ({
    instruction: 'evaluate_sell',
    asset_type: 'ETF',
    direction: '卖出',
    cur_weight: 20.0,
    tgt_weight: 15.0,
    delta_weight: -5.0,
    cur_shares: 40000,
    tgt_shares: 22000,
    lots: -180,
    limit_ref: 4.432,
    band_low: 4.377,
    band_high: 4.487,
    state: 'below',
    settle: 'T+1',
    reason: '引擎B：跌破 MA20，触发减仓',
    evidence: 'mom_20d -3.2%',
    ...over,
  });
  const orders = [
    mk({ secid: '510300.SH', name: '沪深300ETF', price: 4.432, order_shares: -18000, order_amount: 79776, fee: 20.0 }),
    mk({ secid: '518880.SH', name: '黄金ETF', price: 8.632, order_shares: -9300, order_amount: 80287, fee: 20.01, settle: 'T+0' }),
  ];
  return {
    id: pf.id * 1000 + 1,
    exec_uid: 'EX-20260930-MULTI5',
    plan_id: plan.id,
    plan_uid: plan.plan_uid,
    portfolio_id: pf.id,
    capital_cny: capital,
    status: 'draft',
    created_at: CREATED_AT,
    confirmed_at: null,
    confirmed_by: null,
    filled_at: null,
    fills: null,
    sheet: {
      exec_id: 'EX-20260930-MULTI5',
      portfolio: pf.pid,
      portfolio_name: pf.name,
      plan_id: plan.plan_uid,
      as_of: AS_OF,
      next_trade_date: NEXT_TRADE_DATE,
      holiday_days: 3,
      capital_cny: capital,
      base_ccy: 'CNY',
      generated_at: CREATED_AT,
      status: 'draft',
      discipline: '引擎永不直接动仓位：请在券商端依据本执行单人工下单。',
      orders,
      actionable: orders,
      cash: {
        sell_proceeds: 160063,
        buy_outlay: 0,
        net_cash: 160063,
        end_invested_cny: 841600,
        end_invested_pct: 84.2,
        end_cash_cny: 158400,
        end_cash_pct: 15.8,
      },
      fees: { commission_rate: '0.00025', min_fee_cny: 5, total_fee_cny: 40.01, note: '佣金万2.5、最低¥5；ETF 免印花税' },
      summary: { n_orders: 2, n_sell: 2, n_buy: 0, turnover_cny: 160063, turnover_pct: 16.0 },
      checks: [{ item: '整手', detail: '全部 100 份整数倍', status: 'pass' }],
      risks: ['QDII 场内价可偏离净值，下单前核对 IOPV 溢价'],
    },
    timeline: [{ at: CREATED_AT, action: 'create', actor: 'demo', payload: {} }],
  };
}

/* ── 校验夹具 ───────────────────────────────────────────────── */

function verificationChecks() {
  const detail: Record<string, { text: string; [k: string]: unknown }> = {
    pit_audit: { text: 'PIT 阶梯审计通过：无未来函数', count: 0 },
    golden_test: { text: 'golden 99 项对账全绿', count: 99 },
    cross_source: { text: '新浪/腾讯交叉源 90 点一致', sources: 90 },
    calendar_check: { text: '交易日历校验通过，停牌豁免 3 日', exempt: 3 },
    adjust_check: { text: '复权一致性 5531 点，最大偏差 0.2339%', count: 5531, max_diff: 0.2339 },
    provenance_check: { text: '溯源字段完整', sources: 9 },
  };
  return (['pit_audit', 'golden_test', 'cross_source', 'calendar_check', 'adjust_check', 'provenance_check'] as const).map(
    (check, i) => ({
      id: i + 1,
      job_id: 'job-verify-1',
      check,
      status: 'pass' as const,
      detail: detail[check],
      created_at: CREATED_AT,
    }),
  );
}

/* ── 有状态 mock store ──────────────────────────────────────── */

export interface MockStore {
  scenario: Scenario;
  portfolios: PortfolioFixture[];
  runs: Array<Record<string, unknown>>;
  artifacts: Record<string, unknown>;
  plans: Array<ReturnType<typeof buildPlan>>;
  execOrders: Array<ReturnType<typeof buildExecOrder>>;
  jobs: Record<string, Record<string, unknown>>;
  asOf: string;
  gateFail: boolean;
  jobLocked: boolean;
  loginLocked: boolean;
  seq: number;
}

export function createStore(scenario: Scenario): MockStore {
  return {
    scenario,
    // 剧本 A 从仅含 cn4 的 seed 出发，随后新建 multi5；剧本 B 直接用 seed 的 cn4。
    portfolios: scenario === 'multi5' ? [CN4_PORTFOLIO] : [CN4_PORTFOLIO, MULTI5_PORTFOLIO],
    runs: [],
    artifacts: {},
    plans: [],
    execOrders: [],
    jobs: {},
    asOf: AS_OF,
    gateFail: false,
    jobLocked: false,
    loginLocked: false,
    seq: 0,
  };
}

/* ── HTTP 辅助 ──────────────────────────────────────────────── */

function ok(route: Route, body: unknown, status = 200): Promise<void> {
  return route.fulfill({
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
    body: JSON.stringify(body),
  });
}

function fail(route: Route, status: number, code: string, message: string, detail?: unknown): Promise<void> {
  return ok(route, { code, message, detail }, status);
}

async function readBody(route: Route): Promise<Record<string, unknown> | null> {
  try {
    return route.request().postDataJSON() as Record<string, unknown>;
  } catch {
    return null;
  }
}

function findPf(store: MockStore, pid: string): PortfolioFixture | undefined {
  return store.portfolios.find((p) => p.pid === pid);
}

function pfOut(store: MockStore, pf: PortfolioFixture, withPositions = true) {
  return {
    ...pf,
    position_count: pf.positions.length,
    latest_run: latestRunSummary(store, pf),
    positions: withPositions ? pf.positions : undefined,
  };
}

/** 最近一次 run 摘要，严格对齐 core ``portfolios.latest_run``：
 * ``{run_uid, type, as_of, created_at, summary}``（四指标在 ``summary.portfolio_metrics``）。 */
function latestRunSummary(store: MockStore, pf: PortfolioFixture): Record<string, unknown> | null {
  const r = store.runs.find((x) => x.portfolio_id === pf.id);
  if (!r) return null;
  return {
    run_uid: r.run_uid,
    type: r.type,
    as_of: r.as_of,
    created_at: r.created_at,
    summary: (r.summary as Record<string, unknown> | undefined) ?? {},
  };
}

function buildStatus(store: MockStore) {
  const checks = verificationChecks();
  return {
    as_of: store.asOf,
    universe: { count: ALL_SECURITIES.length, types: { STOCK: CN4.length, ETF: 4, LOF: 1 } },
    securities: ALL_SECURITIES.map((s) => ({
      secid: s.secid,
      code: s.code,
      name: s.name,
      asset_type: s.asset_type,
      last_bar: store.asOf,
      lag_days: 0,
      missing: 0,
      has_bars: true,
    })),
    verification: {
      status: 'pass',
      job_id: 'job-verify-1',
      created_at: CREATED_AT,
      checks: checks.map((c) => ({ check: c.check, status: c.status, detail: c.detail })),
      failed: [],
    },
    latest_sync: { job_id: 'job-sync-1', status: 'done', progress: 100, finished_at: CREATED_AT },
  };
}

/* ── 路由分发 ───────────────────────────────────────────────── */

export async function installApiMocks(page: Page, store: MockStore): Promise<void> {
  // 仅拦截真正的后端调用（pathname 以 /api/ 开头）。不能用 '**/api/**'：
  // 该 glob 会误命中 Vite 提供的前端源码模块 /src/api/*.ts，导致应用无法挂载。
  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
      try {
        await handle(route, store);
      } catch (err) {
        await fail(route, 500, 'MOCK_ERROR', String(err));
      }
    },
  );
}

async function handle(route: Route, store: MockStore): Promise<void> {
  const req = route.request();
  const method = req.method().toUpperCase();
  const url = new URL(req.url());
  const path = url.pathname.replace(/^\/api/, '');
  const body = await readBody(route);
  let m: RegExpMatchArray | null;

  /* ── 认证 ── */
  if (method === 'POST' && path === '/auth/login') {
    if (store.loginLocked) {
      return fail(route, 429, 'ACCOUNT_LOCKED', '登录失败次数过多，账号已被临时锁定');
    }
    if (body?.username !== 'demo' || body?.password !== 'alphapocket') {
      return fail(route, 401, 'UNAUTHORIZED', '用户名或密码错误');
    }
    return ok(route, {
      access: 'e2e-access',
      refresh: 'e2e-refresh',
      token_type: 'bearer',
      expires_in: 3600,
      user: { id: 1, username: 'demo', role: 'owner' },
    });
  }
  if (method === 'POST' && path === '/auth/refresh') {
    return ok(route, {
      access: 'e2e-access',
      refresh: 'e2e-refresh',
      token_type: 'bearer',
      expires_in: 3600,
      user: { id: 1, username: 'demo', role: 'owner' },
    });
  }
  if (method === 'GET' && path === '/auth/me') {
    return ok(route, { id: 1, username: 'demo', role: 'owner' });
  }
  if (method === 'GET' && path === '/healthz') return ok(route, { status: 'ok' });

  /* ── 标的 ── */
  if (method === 'GET' && path === '/v1/securities/search') {
    const q = (url.searchParams.get('q') ?? '').trim().toLowerCase();
    const list = q
      ? ALL_SECURITIES.filter(
          (s) => s.code.includes(q) || s.secid.toLowerCase().includes(q) || s.name.toLowerCase().includes(q),
        )
      : ALL_SECURITIES.slice(0, 20);
    return ok(route, list.map(securityOut));
  }
  if (method === 'POST' && path === '/v1/securities/register') {
    const code = String(body?.code ?? '');
    const exist =
      ALL_SECURITIES.find((s) => s.code === code) ??
      ALL_SECURITIES.find((s) => s.secid.startsWith(code));
    if (exist) return ok(route, securityOut(exist));
    const market = code.startsWith('6') ? 'SH' : 'SZ';
    return ok(
      route,
      securityOut({
        secid: `${code}.${market}`,
        code,
        name: `新标的${code}`,
        asset_type: (body?.asset_type as 'STOCK') ?? 'STOCK',
        industry: '未知',
        sina_symbol: `${market.toLowerCase()}${code}`,
        ftype: null,
        settle: STOCK_SETTLE,
        cost: STOCK_COST,
      }),
    );
  }

  /* ── 数据 ── */
  if (method === 'GET' && path === '/v1/data/status') return ok(route, buildStatus(store));
  if (method === 'GET' && path === '/v1/data/verifications') {
    return ok(route, { items: verificationChecks() });
  }
  if (method === 'POST' && path === '/v1/data/sync') {
    const jobId = `job-sync-${++store.seq}`;
    store.jobs[jobId] = job(jobId, 'data_sync', 100);
    store.asOf = SYNCED_AS_OF;
    store.runs.unshift(run(`run-sync-${store.seq}`, jobId, 'data_sync', null, store.asOf, ['sync.json']));
    store.artifacts[`run-sync-${store.seq}/sync.json`] = { mode: body?.mode, as_of: store.asOf };
    return ok(route, { job_id: jobId, poll: `/api/v1/jobs/${jobId}` }, 202);
  }

  /* ── 组合 ── */
  if (method === 'GET' && path === '/v1/portfolios') {
    return ok(route, store.portfolios.map((pf) => pfOut(store, pf, false)));
  }
  if (method === 'POST' && path === '/v1/portfolios') {
    const positions = (body?.positions as Array<{ secid: string; weight: number }>) ?? [];
    if (!positions.length) return fail(route, 422, 'VALIDATION_ERROR', '至少需要一个持仓');
    const pid = String(body?.pid ?? `pf_${store.portfolios.length + 1}`);
    if (store.portfolios.some((p) => p.pid === pid)) {
      return fail(route, 409, 'CONFLICT', `pid 已存在：${pid}`);
    }
    const secMap = new Map(ALL_SECURITIES.map((s) => [s.secid, s]));
    const pf: PortfolioFixture = {
      id: Math.max(...store.portfolios.map((p) => p.id)) + 1,
      pid,
      name: String(body?.name ?? pid),
      note: String(body?.note ?? ''),
      base_ccy: 'CNY',
      strategy: {},
      status: 'active',
      created_at: CREATED_AT,
      updated_at: CREATED_AT,
      positions: positions.map((p) => {
        const s = secMap.get(p.secid);
        return {
          secid: p.secid,
          weight: Number(p.weight),
          name: s?.name ?? p.secid,
          asset_type: s?.asset_type ?? 'STOCK',
          settle: s?.settle,
          cost: s?.cost,
        };
      }),
    };
    store.portfolios.push(pf);
    return ok(route, pfOut(store, pf), 201);
  }

  if ((m = path.match(/^\/v1\/portfolios\/([^/]+)$/))) {
    const pf = findPf(store, m[1]);
    if (!pf) return fail(route, 404, 'NOT_FOUND', `组合不存在：${m[1]}`);
    if (method === 'GET') return ok(route, pfOut(store, pf));
    if (method === 'DELETE') {
      pf.status = 'archived';
      return ok(route, pfOut(store, pf));
    }
  }

  /* ── 评估 / 回测 / 调仓（门禁与互斥分支） ── */
  for (const [suffix] of [['/review'], ['/backtest'], ['/rebalance-plan']] as const) {
    const re = new RegExp(`^\\/v1\\/portfolios\\/([^/]+)${suffix}$`);
    if (method === 'POST' && (m = path.match(re))) {
      const pf = findPf(store, m[1]);
      if (!pf) return fail(route, 404, 'NOT_FOUND', `组合不存在：${m[1]}`);
      const gate = checkGate(store, route);
      if (gate) return gate;
      if (suffix === '/review') {
        const uid = `run-review-${pf.pid}`;
        const review = buildReview(pf);
        upsertRun(store, uid, 'review', pf.id, AS_OF, ['review.json', 'report.html', 'report.md'], {
          as_of: review.as_of,
          portfolio_metrics: review.portfolio_metrics,
          position_count: review.positions.length,
          signal_count: review.signals.length,
          signal_states: {},
        });
        store.artifacts[`${uid}/review.json`] = review;
        store.artifacts[`${uid}/report.html`] = `<html><body><h1>${pf.name} 报告</h1></body></html>`;
        store.artifacts[`${uid}/report.md`] = `# ${pf.name} 报告`;
        return accepted(store, route, 'review', pf.pid);
      }
      if (suffix === '/backtest') {
        const uid = `run-backtest-${pf.pid}`;
        const bt = buildBacktest(pf);
        upsertRun(store, uid, 'backtest', pf.id, AS_OF, ['backtest.json'], {
          n_rebalances: bt.n_rebalances,
          total_return: bt.strategy_metrics.total_return,
        });
        store.artifacts[`${uid}/backtest.json`] = bt;
        return accepted(store, route, 'backtest', pf.pid);
      }
      // rebalance-plan
      const plan = buildPlan(pf);
      store.plans = [plan, ...store.plans.filter((p) => p.portfolio_id !== pf.id)];
      return accepted(store, route, 'rebalance_plan', pf.pid);
    }
  }

  /* ── plans / exec-orders ── */
  if (method === 'GET' && (m = path.match(/^\/v1\/portfolios\/([^/]+)\/plans$/))) {
    const pf = findPf(store, m[1]);
    if (!pf) return fail(route, 404, 'NOT_FOUND', `组合不存在：${m[1]}`);
    return ok(
      route,
      store.plans
        .filter((p) => p.portfolio_id === pf.id)
        .map((p) => ({
          id: p.id,
          plan_uid: p.plan_uid,
          portfolio_id: p.portfolio_id,
          run_id: p.run_id,
          status: p.status,
          cost_est_cny: p.payload.cost_est_cny,
          created_at: p.created_at,
        })),
    );
  }
  if (method === 'GET' && (m = path.match(/^\/v1\/plans\/([^/]+)$/))) {
    const plan = store.plans.find((p) => String(p.id) === m![1]);
    if (!plan) return fail(route, 404, 'NOT_FOUND', `plan 不存在：${m[1]}`);
    return ok(route, plan);
  }
  if (method === 'POST' && (m = path.match(/^\/v1\/plans\/([^/]+)\/exec-orders$/))) {
    const plan = store.plans.find((p) => String(p.id) === m![1]);
    if (!plan) return fail(route, 404, 'NOT_FOUND', `plan 不存在：${m[1]}`);
    const pf = store.portfolios.find((p) => p.id === plan.portfolio_id)!;
    const order = buildExecOrder(pf, plan, Number(body?.capital_cny ?? 1_000_000));
    store.execOrders = [order, ...store.execOrders.filter((o) => o.plan_id !== plan.id)];
    store.artifacts[`${order.exec_uid}/exec_order.csv`] = 'secid,direction,shares,price\n510300.SH,SELL,18000,4.432\n';
    return ok(route, order, 201);
  }
  if (method === 'GET' && (m = path.match(/^\/v1\/portfolios\/([^/]+)\/exec-orders$/))) {
    const pf = findPf(store, m[1]);
    if (!pf) return fail(route, 404, 'NOT_FOUND', `组合不存在：${m[1]}`);
    return ok(
      route,
      store.execOrders
        .filter((o) => o.portfolio_id === pf.id)
        .map((o) => ({
          id: o.id,
          exec_uid: o.exec_uid,
          plan_id: o.plan_id,
          portfolio_id: o.portfolio_id,
          capital_cny: o.capital_cny,
          status: o.status,
          created_at: o.created_at,
          confirmed_at: o.confirmed_at,
          filled_at: o.filled_at,
        })),
    );
  }
  if (method === 'GET' && (m = path.match(/^\/v1\/exec-orders\/([^/]+)$/))) {
    const o = store.execOrders.find((x) => x.exec_uid === m![1]);
    if (!o) return fail(route, 404, 'NOT_FOUND', `执行单不存在：${m[1]}`);
    return ok(route, o);
  }
  if (method === 'POST' && (m = path.match(/^\/v1\/exec-orders\/([^/]+)\/(confirm|fill|cancel)$/))) {
    const o = store.execOrders.find((x) => x.exec_uid === m![1]);
    if (!o) return fail(route, 404, 'NOT_FOUND', `执行单不存在：${m[1]}`);
    const action = m[2];
    if (action === 'confirm') {
      if (body?.ack_discipline !== true) return fail(route, 422, 'ACK_REQUIRED', '须确认纪律声明');
      if (o.status !== 'draft') return fail(route, 409, 'INVALID_STATE', '仅 draft 可确认');
      o.status = 'confirmed';
      o.confirmed_at = CREATED_AT;
      o.confirmed_by = 'demo';
      o.sheet.status = 'confirmed';
      o.timeline.push({ at: CREATED_AT, action: 'confirm', actor: 'demo', payload: { ack_discipline: true } });
      return ok(route, o);
    }
    if (action === 'fill') {
      if (o.status !== 'confirmed') return fail(route, 409, 'INVALID_STATE', '仅 confirmed 可登记成交');
      const fills = (body?.fills as Array<{ secid: string; price: number; shares: number }>) ?? [];
      o.status = 'executed';
      o.filled_at = CREATED_AT;
      o.sheet.status = 'executed';
      o.fills = {
        items: fills.map((f) => {
          const row = o.sheet.orders.find((r) => r.secid === f.secid)!;
          return {
            secid: f.secid,
            name: row.name,
            direction: row.direction,
            price: f.price,
            shares: f.shares,
            order_shares: row.order_shares,
            amount: Number((Math.abs(f.shares) * f.price).toFixed(2)),
          };
        }),
        n: fills.length,
        total_amount: 160063,
        partial: false,
      };
      o.timeline.push({ at: CREATED_AT, action: 'fill', actor: 'demo', payload: { n: fills.length } });
      return ok(route, o);
    }
    o.status = 'cancelled';
    o.sheet.status = 'cancelled';
    o.timeline.push({ at: CREATED_AT, action: 'cancel', actor: 'demo', payload: {} });
    return ok(route, o);
  }
  if (method === 'GET' && (m = path.match(/^\/v1\/portfolios\/([^/]+)\/rebalance-history$/))) {
    return ok(route, { portfolio_id: m[1], items: [], count: 0 });
  }

  /* ── runs / jobs ── */
  if (method === 'GET' && path === '/v1/runs') {
    const pid = url.searchParams.get('portfolio_id');
    // 严格复刻 FastAPI：portfolio_id 为 int，'NaN'/非数字 → 422（防 Number(pid) 回归）
    if (pid !== null && !/^-?\d+$/.test(pid)) {
      return fail(route, 422, 'VALIDATION_ERROR', 'portfolio_id 必须为整数', { loc: ['query', 'portfolio_id'] });
    }
    const type = url.searchParams.get('type');
    const limit = Number(url.searchParams.get('limit') ?? '20');
    let list = [...store.runs];
    if (type) list = list.filter((r) => r.type === type);
    if (pid !== null) list = list.filter((r) => r.portfolio_id === Number(pid));
    return ok(route, list.slice(0, limit));
  }
  if (method === 'GET' && (m = path.match(/^\/v1\/runs\/([^/]+)\/artifacts\/(.+)$/))) {
    const key = `${m[1]}/${m[2]}`;
    if (!(key in store.artifacts)) return fail(route, 404, 'NOT_FOUND', `产物不存在：${key}`);
    const value = store.artifacts[key];
    if (typeof value === 'string') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/plain; charset=utf-8' }, body: value });
    }
    return ok(route, value);
  }
  if (method === 'GET' && (m = path.match(/^\/v1\/jobs\/([^/]+)$/))) {
    const job = store.jobs[m[1]];
    if (!job) return fail(route, 404, 'NOT_FOUND', `job 不存在：${m[1]}`);
    return ok(route, job);
  }

  return fail(route, 404, 'NOT_FOUND', `未 mock 的端点：${method} ${path}`);
}

/* ── 分支与工厂辅助 ─────────────────────────────────────────── */

function checkGate(store: MockStore, route: Route): Promise<void> | null {
  if (store.gateFail) {
    return fail(route, 409, 'VERIFY_GATE_FAILED', '数据验证未全绿，计算被门禁拦截', {
      failed: ['golden_test', 'adjust_check'],
    });
  }
  if (store.jobLocked) {
    return fail(route, 409, 'JOB_LOCKED', '同 workspace 有计算任务进行中');
  }
  return null;
}

function job(id: string, type: string, progress: number, status = 'done') {
  return {
    id,
    type,
    portfolio_id: null,
    status,
    params: {},
    result: null,
    error: null,
    progress,
    started_at: CREATED_AT,
    finished_at: CREATED_AT,
    created_at: CREATED_AT,
  };
}

function accepted(store: MockStore, route: Route, type: string, pid: string): Promise<void> {
  const jobId = `job-${type}-${++store.seq}`;
  store.jobs[jobId] = { ...job(jobId, type, 100), params: { pid } };
  return ok(route, { job_id: jobId, poll: `/api/v1/jobs/${jobId}` }, 202);
}

function run(
  uid: string,
  jobId: string,
  type: string,
  portfolioId: number | null,
  asOf: string,
  artifactKeys: string[],
  summary: Record<string, unknown> = {},
) {
  return {
    id: uid.split('-').pop(),
    run_uid: uid,
    job_id: jobId,
    portfolio_id: portfolioId,
    type,
    as_of: asOf,
    summary,
    artifact_keys: artifactKeys,
    created_at: CREATED_AT,
  };
}

function upsertRun(
  store: MockStore,
  uid: string,
  type: string,
  portfolioId: number,
  asOf: string,
  artifactKeys: string[],
  summary: Record<string, unknown> = {},
): void {
  store.runs = store.runs.filter((r) => r.run_uid !== uid);
  store.runs.unshift(run(uid, `job-${uid}`, type, portfolioId, asOf, artifactKeys, summary));
}
