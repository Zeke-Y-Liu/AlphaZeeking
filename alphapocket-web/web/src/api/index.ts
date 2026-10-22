/**
 * API 端点封装（路径常量 + 函数签名，对齐实施书 §6.2）。
 *
 * 类型定义见 ./types.ts；本章仅封装端点路径与请求/响应解包。
 */
import { apiClient } from './client';
import type {
  AnalysisNews,
  AnalysisProfile,
  AssetType,
  AuthTokens,
  AuthUser,
  BacktestRequest,
  CreatePortfolioRequest,
  DataStatus,
  DataSyncRequest,
  EngineDescribe,
  EngineMetricAddRequest,
  EngineMetricUpdateRequest,
  EngineMetricsResponse,
  EngineDatasetsResponse,
  ExecOrder,
  ExecOrderFill,
  ExecOrderSummary,
  Job,
  JobAccepted,
  MetricAddResponse,
  MetricRemoveResponse,
  MetricUpdateResponse,
  Portfolio,
  QuotesResponse,
  RebalancePlan,
  RebalancePlanSummary,
  Run,
  Security,
  VerifyResult,
} from './types';

/**
 * 路径常量。apiClient.baseURL='/api'。
 * 认证端点为 BFF 直接暴露（/auth/*，见 T5.2 冻结契约，非 core 的 /v1 前缀）；
 * 其余为 core 前缀 /v1，BFF 原样透传。
 */
export const API_PATHS = {
  healthz: '/healthz',
  authLogin: '/auth/login',
  authRefresh: '/auth/refresh',
  authMe: '/auth/me',
  securitiesSearch: '/v1/securities/search',
  securitiesRegister: '/v1/securities/register',
  marketQuotes: '/v1/market/quotes',
  marketGgt: '/v1/market/ggt',
  dataSync: '/v1/data/sync',
  dataStatus: '/v1/data/status',
  dataVerifications: '/v1/data/verifications',
  portfolios: '/v1/portfolios',
  portfolio: (id: number | string) => `/v1/portfolios/${id}`,
  portfolioReview: (id: number | string) => `/v1/portfolios/${id}/review`,
  portfolioBacktest: (id: number | string) => `/v1/portfolios/${id}/backtest`,
  portfolioRebalancePlan: (id: number | string) => `/v1/portfolios/${id}/rebalance-plan`,
  portfolioPlans: (id: number | string) => `/v1/portfolios/${id}/plans`,
  plan: (id: number | string) => `/v1/plans/${id}`,
  planExecOrders: (id: number | string) => `/v1/plans/${id}/exec-orders`,
  portfolioExecOrders: (id: number | string) => `/v1/portfolios/${id}/exec-orders`,
  rebalanceHistory: (id: number | string) => `/v1/portfolios/${id}/rebalance-history`,
  execOrder: (id: number | string) => `/v1/exec-orders/${id}`,
  execOrderConfirm: (id: number | string) => `/v1/exec-orders/${id}/confirm`,
  execOrderFill: (id: number | string) => `/v1/exec-orders/${id}/fill`,
  execOrderCancel: (id: number | string) => `/v1/exec-orders/${id}/cancel`,
  runs: '/v1/runs',
  runArtifacts: (uid: string, file: string) => `/v1/runs/${uid}/artifacts/${file}`,
  job: (id: string) => `/v1/jobs/${id}`,
  // 引擎板块（指标注册表 CRUD + 数据集联动）
  enginesMetrics: '/v1/engines/metrics',
  engineMetric: (mid: string) => `/v1/engines/metrics/${mid}`,
  enginesDescribe: '/v1/engines/describe',
  enginesDatasets: '/v1/engines/datasets',
  // 单公司分析（M10）
  analyses: '/v1/analysis',
  analysis: (aid: string) => `/v1/analysis/${aid}`,
  analysisRun: (aid: string) => `/v1/analysis/${aid}/run`,
  analysisNews: (aid: string) => `/v1/analysis/${aid}/news`,
  analysisNewsRefresh: (aid: string) => `/v1/analysis/${aid}/news/refresh`,
  analysisBacktest: (aid: string) => `/v1/analysis/${aid}/backtest`,
  // 单标的手动数据拉取（组合/公司页面入口）
  dataSyncSecid: '/v1/data/sync-secid',
} as const;

/* ── 已实现 ─────────────────────────────────────── */
/** GET /healthz：存活检查。 */
export async function healthz(): Promise<{ status: string }> {
  const { data } = await apiClient.get<{ status: string }>(API_PATHS.healthz);
  return data;
}

/* ── 认证（T5.2 · 已实现） ──────────────────────── */
/** POST /auth/login：用户名+密码 → token 三件套 + user。 */
export async function authLogin(body: { username: string; password: string }): Promise<AuthTokens> {
  const { data } = await apiClient.post<AuthTokens>(API_PATHS.authLogin, body);
  return data;
}

/** POST /auth/refresh：refresh token → 新 token 三件套 + user。 */
export async function authRefresh(body: { refresh: string }): Promise<AuthTokens> {
  const { data } = await apiClient.post<AuthTokens>(API_PATHS.authRefresh, body);
  return data;
}

/** GET /auth/me：当前用户（需 Bearer access）。 */
export async function authMe(): Promise<AuthUser> {
  const { data } = await apiClient.get<AuthUser>(API_PATHS.authMe);
  return data;
}

/* ── 标的（T1.3 · 已实现） ──────────────────────── */
/** GET /v1/securities/search?q=：代码/名称/secid 前缀检索（空 q 返回最近 20）。 */
export async function searchSecurities(q: string): Promise<Security[]> {
  const { data } = await apiClient.get<Security[]>(API_PATHS.securitiesSearch, { params: { q } });
  return data;
}

/** POST /v1/securities/register：注册标的（可选 asset_type 覆盖判定），已存在幂等返回。 */
export async function registerSecurity(code: string, assetType?: AssetType): Promise<Security> {
  const { data } = await apiClient.post<Security>(API_PATHS.securitiesRegister, {
    code,
    asset_type: assetType,
  });
  return data;
}

/* ── 实时行情（V4-β 多市场） ──────────────────── */
/**
 * GET /v1/market/quotes?secids=：A股/港股/美股混合批量实时行情 + 汇率快照。
 * 服务端 Redis 5s 缓存；双源失败条目带 error 字段。
 */
export async function getQuotes(secids: string[]): Promise<QuotesResponse> {
  const { data } = await apiClient.get<QuotesResponse>(API_PATHS.marketQuotes, {
    params: { secids: secids.join(',') },
  });
  return data;
}

/** GET /v1/market/ggt：港股通标的名单（雪球在线 → 静态快照降级）。 */
export async function getGgtComponents(): Promise<{
  codes: string[];
  count: number;
  source: string;
}> {
  const { data } = await apiClient.get<{
    codes: string[];
    count: number;
    source: string;
  }>(API_PATHS.marketGgt);
  return data;
}

/* ── 数据（T1.4/T1.5） ──────────────────────────── */
/**
 * POST /v1/data/sync：触发同步 job（202 → {job_id, poll}）。
 * 422 mode 非法 / refresh 缺 secid；409 JOB_LOCKED；503 上游熔断。
 */
export async function syncData(body: DataSyncRequest): Promise<JobAccepted> {
  const { data } = await apiClient.post<JobAccepted>(API_PATHS.dataSync, body);
  return data;
}

/** GET /v1/data/status：universe 状态 + 六项验证汇总 + 最近 sync job。 */
export async function getDataStatus(): Promise<DataStatus> {
  const { data } = await apiClient.get<DataStatus>(API_PATHS.dataStatus);
  return data;
}

/** GET /v1/data/verifications?limit=：验证明细列表（含 detail）。 */
export async function listVerifications(limit = 20): Promise<VerifyResult[]> {
  const { data } = await apiClient.get<{ items: VerifyResult[] }>(API_PATHS.dataVerifications, {
    params: { limit },
  });
  return data.items;
}

/* ── 组合（T2.2 · 已实现） ──────────────────────── */
/** GET /v1/portfolios：组合列表（含持仓数与最近 run 摘要）。 */
export async function listPortfolios(): Promise<Portfolio[]> {
  const { data } = await apiClient.get<Portfolio[]>(API_PATHS.portfolios);
  return data;
}

/** POST /v1/portfolios：新建（服务端做标的/权重窗口校验，越界 422）。 */
export async function createPortfolio(body: CreatePortfolioRequest): Promise<Portfolio> {
  const { data } = await apiClient.post<Portfolio>(API_PATHS.portfolios, body);
  return data;
}

/** GET /v1/portfolios/{id}：详情（含 positions）。 */
export async function getPortfolio(id: number | string): Promise<Portfolio> {
  const { data } = await apiClient.get<Portfolio>(API_PATHS.portfolio(id));
  return data;
}

/** PUT /v1/portfolios/{id}：更新（重跑校验 + 重新物化）。 */
export async function updatePortfolio(
  id: number | string,
  body: Partial<CreatePortfolioRequest>,
): Promise<Portfolio> {
  const { data } = await apiClient.put<Portfolio>(API_PATHS.portfolio(id), body);
  return data;
}

/** DELETE /v1/portfolios/{id}：归档软删（status=archived）。 */
export async function deletePortfolio(id: number | string): Promise<void> {
  await apiClient.delete(API_PATHS.portfolio(id));
}

/* ── 评估 / 回测 / 调仓（T2.3 / T3.2 / T4.x） ──────── */
/**
 * POST /v1/portfolios/{id}/review：异步评估（202 → {job_id, poll, data_risk}）。
 * 数据门禁不阻断评估：未全绿时 data_risk 提示数据风险（全绿为 null）；
 * workspace 计算互斥 → 409 JOB_LOCKED。
 */
export async function reviewPortfolio(id: number | string): Promise<JobAccepted> {
  const { data } = await apiClient.post<JobAccepted>(API_PATHS.portfolioReview(id));
  return data;
}

/**
 * POST /v1/portfolios/{id}/backtest：异步回测（202 → {job_id, poll}）。
 * 门禁未全绿 → 409 VERIFY_GATE_FAILED；参数非法 → 422 VALIDATION_ERROR。
 */
export async function backtestPortfolio(
  id: number | string,
  body: BacktestRequest,
): Promise<JobAccepted> {
  const { data } = await apiClient.post<JobAccepted>(API_PATHS.portfolioBacktest(id), body);
  return data;
}

/**
 * POST /v1/portfolios/{id}/rebalance-plan：触发调仓评估单 job（202）。
 * 门禁未全绿 409 VERIFY_GATE_FAILED；互斥 409 JOB_LOCKED。
 */
export async function createRebalancePlan(id: number | string): Promise<JobAccepted> {
  const { data } = await apiClient.post<JobAccepted>(API_PATHS.portfolioRebalancePlan(id));
  return data;
}

/** GET /v1/portfolios/{id}/plans：plan 列表（draft 优先，不含 payload）。 */
export async function listPlans(id: number | string): Promise<RebalancePlanSummary[]> {
  const { data } = await apiClient.get<RebalancePlanSummary[]>(API_PATHS.portfolioPlans(id));
  return data;
}

/** GET /v1/plans/{id}：plan 详情（含完整 payload.diff_trades）。 */
export async function getPlan(id: number | string): Promise<RebalancePlan> {
  const { data } = await apiClient.get<RebalancePlan>(API_PATHS.plan(id));
  return data;
}

/**
 * POST /v1/plans/{plan_id}/exec-orders：由 plan + 最新 review 机械换算执行单（同步）。
 * plan 过老 → 409 DATA_STALE；plan 不存在 → 404。
 */
export async function createExecOrders(
  planId: number | string,
  body: { capital_cny: number },
): Promise<ExecOrder> {
  const { data } = await apiClient.post<ExecOrder>(API_PATHS.planExecOrders(planId), body);
  return data;
}

/** POST /v1/exec-orders/{id}/confirm：纪律 ACK（false → 422 ACK_REQUIRED）→ confirmed。 */
export async function confirmExecOrder(
  id: number | string,
  body: { ack_discipline: boolean },
): Promise<ExecOrder> {
  const { data } = await apiClient.post<ExecOrder>(API_PATHS.execOrderConfirm(id), body);
  return data;
}

/** POST /v1/exec-orders/{id}/fill：登记成交（整手/方向校验）→ executed（允许部分成交）。 */
export async function fillExecOrder(
  id: number | string,
  body: { fills: ExecOrderFill[] },
): Promise<ExecOrder> {
  const { data } = await apiClient.post<ExecOrder>(API_PATHS.execOrderFill(id), body);
  return data;
}

/** POST /v1/exec-orders/{id}/cancel：取消（draft/confirmed → cancelled）。 */
export async function cancelExecOrder(
  id: number | string,
  body: { reason?: string } = {},
): Promise<ExecOrder> {
  const { data } = await apiClient.post<ExecOrder>(API_PATHS.execOrderCancel(id), body);
  return data;
}

/** GET /v1/exec-orders/{id}：执行单详情（含完整 sheet 与状态时间线）。 */
export async function getExecOrder(id: number | string): Promise<ExecOrder> {
  const { data } = await apiClient.get<ExecOrder>(API_PATHS.execOrder(id));
  return data;
}

/** GET /v1/portfolios/{pid}/exec-orders：该组合执行单列表（不含大 sheet）。 */
export async function listExecOrders(pid: number | string): Promise<ExecOrderSummary[]> {
  const { data } = await apiClient.get<ExecOrderSummary[]>(API_PATHS.portfolioExecOrders(pid));
  return data;
}

/** GET /v1/portfolios/{pid}/rebalance-history：plans/orders/fills 归一化时间线。 */
export async function rebalanceHistory(
  pid: number | string,
): Promise<{ portfolio_id: string; items: Record<string, unknown>[]; count: number }> {
  const { data } = await apiClient.get<{
    portfolio_id: string;
    items: Record<string, unknown>[];
    count: number;
  }>(API_PATHS.rebalanceHistory(pid));
  return data;
}

/* ── 运行历史 / Job ─────────────────────────────── */
/** GET /v1/runs?type=&portfolio_id=&limit=：运行历史（最新在前）。 */
export async function listRuns(params?: {
  type?: string;
  portfolio_id?: number;
  limit?: number;
}): Promise<Run[]> {
  const { data } = await apiClient.get<Run[]>(API_PATHS.runs, { params });
  return data;
}

/** GET /v1/jobs/{id}：job 状态/进度（终态 status ∈ done/failed）。 */
export async function getJob(id: string): Promise<Job> {
  const { data } = await apiClient.get<Job>(API_PATHS.job(id));
  return data;
}

/** 产物下载/嵌入 URL（直接给 iframe / a[href] 使用，不走 axios）。 */
export function runArtifactUrl(runUid: string, file: string): string {
  const base = import.meta.env.VITE_API_BASE ?? '/api';
  return `${base}${API_PATHS.runArtifacts(runUid, file)}`;
}

/**
 * 鉴权取用 run 产物（review.json / report.html / report.md）——BFF 全局鉴权要求 Bearer，
 * 而 iframe/a[href] 无法携带，故统一经 apiClient 取回（text/json/blob），前端再绑定
 * `srcdoc` / `URL.createObjectURL`。调用方需对 404（产物尚不存在）做容错。
 *
 * 注意：此处不复用 `runArtifactUrl`（其已含 baseURL 前缀，交给 axios 会二次拼接），
 * 直接用 `API_PATHS.runArtifacts` 相对路径，由 apiClient 补 baseURL。
 */
export async function fetchRunArtifact<T = unknown>(
  runUid: string,
  file: string,
  responseType: 'text' | 'json' | 'blob',
): Promise<T> {
  const { data } = await apiClient.get<T>(API_PATHS.runArtifacts(runUid, file), { responseType });
  return data;
}

/* ── 引擎板块（指标注册表 CRUD + 数据集联动） ────── */
/** GET /v1/engines/metrics?engine=&status=：指标清单（含 changelog 计数）。 */
export async function listEngineMetrics(params?: {
  engine?: string;
  status?: string;
}): Promise<EngineMetricsResponse> {
  const { data } = await apiClient.get<EngineMetricsResponse>(API_PATHS.enginesMetrics, { params });
  return data;
}

/** GET /v1/engines/describe：三引擎自描述。 */
export async function describeEngines(): Promise<{ engines: EngineDescribe[] }> {
  const { data } = await apiClient.get<{ engines: EngineDescribe[] }>(API_PATHS.enginesDescribe);
  return data;
}

/** GET /v1/engines/datasets：指标引用数据集清单 + 落盘状态（数据板块共用）。 */
export async function listEngineDatasets(): Promise<EngineDatasetsResponse> {
  const { data } = await apiClient.get<EngineDatasetsResponse>(API_PATHS.enginesDatasets);
  return data;
}

/** POST /v1/engines/metrics：新增指标（hypothesis_id 必填；数据缺失不阻断附提示）。 */
export async function addEngineMetric(body: EngineMetricAddRequest): Promise<MetricAddResponse> {
  const { data } = await apiClient.post<MetricAddResponse>(API_PATHS.enginesMetrics, body);
  return data;
}

/** PATCH /v1/engines/metrics/{mid}：修改（深合并；启停需 hypothesis_id）。 */
export async function updateEngineMetric(
  mid: string,
  body: EngineMetricUpdateRequest,
): Promise<MetricUpdateResponse> {
  const { data } = await apiClient.patch<MetricUpdateResponse>(API_PATHS.engineMetric(mid), body);
  return data;
}

/** DELETE /v1/engines/metrics/{mid}：软删（status=inactive；hypothesis_id 必填）。 */
export async function removeEngineMetric(
  mid: string,
  body: { hypothesis_id: string; reason?: string; engine?: string | null },
): Promise<MetricRemoveResponse> {
  const { data } = await apiClient.delete<MetricRemoveResponse>(API_PATHS.engineMetric(mid), {
    data: body,
  });
  return data;
}

/** POST /v1/data/sync-secid：手动拉取单个标的数据（refresh 近5日，202）。
 * 未注册 422；已有同步在跑 409 JOB_LOCKED；上游熔断 503。
 */
export async function syncSecid(secid: string): Promise<JobAccepted & { secid?: string }> {
  const { data } = await apiClient.post<JobAccepted & { secid?: string }>(
    API_PATHS.dataSyncSecid,
    { secid },
  );
  return data;
}

/* ── 单公司分析（M10） ──────────────────────────── */
/** GET /v1/analysis：档案列表（默认 active）。 */
export async function listAnalyses(params?: {
  include_archived?: boolean;
}): Promise<AnalysisProfile[]> {
  const { data } = await apiClient.get<AnalysisProfile[]>(API_PATHS.analyses, { params });
  return data;
}

/** POST /v1/analysis：建档案（自动建影子组合 shadow_{aid}）。 */
export async function createAnalysis(body: {
  secid: string;
  name?: string;
  note?: string;
  aid?: string;
  modules?: Record<string, boolean>;
}): Promise<AnalysisProfile> {
  const { data } = await apiClient.post<AnalysisProfile>(API_PATHS.analyses, body);
  return data;
}

/** GET /v1/analysis/{aid}：档案详情。 */
export async function getAnalysis(aid: string): Promise<AnalysisProfile> {
  const { data } = await apiClient.get<AnalysisProfile>(API_PATHS.analysis(aid));
  return data;
}

/** PUT /v1/analysis/{aid}：更新（name/note/modules）。 */
export async function updateAnalysis(
  aid: string,
  body: { name?: string; note?: string; modules?: Record<string, boolean> },
): Promise<AnalysisProfile> {
  const { data } = await apiClient.put<AnalysisProfile>(API_PATHS.analysis(aid), body);
  return data;
}

/** DELETE /v1/analysis/{aid}：软删 + 影子组合归档。 */
export async function deleteAnalysis(aid: string): Promise<void> {
  await apiClient.delete(API_PATHS.analysis(aid));
}

/** POST /v1/analysis/{aid}/run：触发四模块分析 job（202）。 */
export async function runAnalysis(aid: string): Promise<JobAccepted> {
  const { data } = await apiClient.post<JobAccepted>(API_PATHS.analysisRun(aid));
  return data;
}

/** GET /v1/analysis/{aid}/news?limit=：最近消息（直读数据集）。 */
export async function getAnalysisNews(aid: string, limit = 10): Promise<AnalysisNews> {
  const { data } = await apiClient.get<AnalysisNews>(API_PATHS.analysisNews(aid), {
    params: { limit },
  });
  return data;
}

/** POST /v1/analysis/{aid}/news/refresh：刷新该标的新闻（小 job，202）。 */
export async function refreshAnalysisNews(aid: string): Promise<JobAccepted> {
  const { data } = await apiClient.post<JobAccepted>(API_PATHS.analysisNewsRefresh(aid));
  return data;
}

/** POST /v1/analysis/{aid}/backtest：影子组合回测（复用组合回测链路，202）。 */
export async function backtestAnalysis(
  aid: string,
  body?: BacktestRequest,
): Promise<JobAccepted & { pid?: string }> {
  const { data } = await apiClient.post<JobAccepted & { pid?: string }>(
    API_PATHS.analysisBacktest(aid),
    body ?? {},
  );
  return data;
}
