<script setup lang="ts">
// 组合详情（T6.4）：Tab 概览（体检卡五指标 / 引擎A 因子表 / 引擎B 信号卡 / 下一交易日指引
// / 数据截至徽章 + 数据风险警示条）+ Tab 报告入口。数据源为最近一次 review run 的 review.json 产物。
// 门禁语义：评估不被数据门禁阻断，非全绿时以 data_risk 提示（琥珀警示条 + toast.warn）。
import { computed, onMounted, ref } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import { fetchRunArtifact, getJob, getPortfolio, listRuns, reviewPortfolio, syncSecid } from '../api';
import { ApMarketBadge } from '../components/ui';
import { ApiError } from '../api/types';
import type {
  DataRisk,
  MacroLiquiditySummary,
  Portfolio,
  ReviewPayload,
  ReviewPosition,
  Run,
} from '../api/types';
import { ApBadge, ApButton, ApCard, ApTable, ApTabs, useToast } from '../components/ui';
import type { ApTableColumn, ApTabItem, BadgeTone } from '../components/ui';
import { useBreakpoint } from '../composables/useBreakpoint';
import { DASH, fmtDate, fmtNum, fmtPct, fmtPctVal } from '../utils/format';

const route = useRoute();
const toast = useToast();
const { isMobile } = useBreakpoint();

const pfId = computed(() => String(route.params.id));

const active = ref('overview');
const tabs: ApTabItem[] = [
  { key: 'overview', label: '概览' },
  { key: 'report', label: '报告' },
];

const pf = ref<Portfolio | null>(null);
const run = ref<Run | null>(null);
const review = ref<ReviewPayload | null>(null);
const loading = ref(false);
const error = ref('');
const evaluating = ref(false);
const progress = ref(0);
/** 数据风险提示（评估不阻断门禁：非全绿时由 202 响应 / run.summary 下发）。 */
const dataRisk = ref<DataRisk | null>(null);
const queued = ref(false);

const positions = computed<ReviewPosition[]>(() => review.value?.positions ?? []);

/** secid → 市场元数据（V4-β：review 产物不含 market，从 core 组合详情的持仓补全）。 */
const secMeta = computed(
  () => new Map((pf.value?.positions ?? []).map((p) => [p.secid, p])),
);

/* ── 体检卡（五指标） ───────────────────────────── */
function retTone(v?: number | null): string {
  if (v == null || !Number.isFinite(v) || v === 0) return '';
  return v > 0 ? 'is-up' : 'is-down';
}

const metrics = computed(() => {
  const m = review.value?.portfolio_metrics;
  return [
    { key: 'day', label: '当日', value: fmtPct(m?.day_ret), tone: retTone(m?.day_ret) },
    { key: '20d', label: '20日', value: fmtPct(m?.ret_20d), tone: retTone(m?.ret_20d) },
    { key: 'ytd', label: 'YTD', value: fmtPct(m?.ret_ytd), tone: retTone(m?.ret_ytd) },
    { key: '1y', label: '1年', value: fmtPct(m?.ret_1y), tone: retTone(m?.ret_1y) },
    { key: 'mdd', label: '1年MDD', value: fmtPct(m?.max_dd_1y), tone: retTone(m?.max_dd_1y) },
  ];
});

/* ── 引擎A 因子表 ──────────────────────────────── */
const aColumns: ApTableColumn[] = [
  { key: 'name', label: '标的' },
  { key: 'weight', label: '权重', align: 'right' },
  { key: 'pe_ttm', label: 'PE(TTM)', align: 'right' },
  { key: 'pb', label: 'PB', align: 'right' },
  { key: 'roe_ttm', label: 'ROE', align: 'right' },
  { key: 'margin', label: '毛利率', align: 'right' },
  { key: 'rev_yoy', label: '营收YoY', align: 'right' },
  { key: 'np_yoy', label: '净利YoY', align: 'right' },
  { key: 'mom_12_1', label: '动量12-1', align: 'right' },
  { key: 'vol_250', label: '波动250', align: 'right' },
  { key: 'div_yield', label: '股息率', align: 'right' },
];

function fmtWeight(v?: number | null): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return `${v.toFixed(1)}%`;
}

const aRows = computed<Record<string, unknown>[]>(() =>
  positions.value.map((p) => {
    const a = p.alpha;
    return {
      secid: p.secid,
      name: p.name || p.secid,
      weight: fmtWeight(p.weight),
      pe_ttm: fmtNum(a?.val_pe_ttm),
      pb: fmtNum(a?.val_pb),
      // ROE/毛利率/成长同比：applib 口径本身即百分数（如 16.1 → 16.1%，见 report.py），
      // 故用 fmtPctVal 而非 fmtPct（后者面向小数比率，会再 ×100）。
      roe_ttm: fmtPctVal(a?.qua_roe_ttm),
      margin: fmtPctVal(a?.qua_margin),
      rev_yoy: fmtPctVal(a?.grow_rev_yoy),
      np_yoy: fmtPctVal(a?.grow_np_yoy),
      mom_12_1: fmtPct(a?.mom_12_1),
      vol_250: fmtPct(a?.vol_250),
      div_yield: fmtPct(a?.div_yield_ttm),
    };
  }),
);

/** 引擎A 口径周期（陈旧度提示）：存在 used_period 时去重展示。 */
const usedPeriods = computed<string[]>(() => {
  const set = new Set<string>();
  for (const p of positions.value) {
    const u = p.alpha?.used_period;
    if (u) set.add(String(u));
  }
  return [...set];
});

/* ── 引擎B 信号卡 ──────────────────────────────── */
const STATE_TONE: Record<string, BadgeTone> = { above: 'danger', below: 'success', neutral: 'default' };

function stateTone(s?: string | null): BadgeTone {
  return s ? (STATE_TONE[s] ?? 'default') : 'default';
}

function boolText(v?: boolean | null): string {
  if (v === true) return '是';
  if (v === false) return '否';
  return DASH;
}

function hasCooldown(secid: string): boolean {
  return (review.value?.signals ?? []).some((s) => s.secid === secid && Boolean(s.cooldown));
}

/* ── 引擎C 宏观流动性（市场级条件变量，不构成买卖信号） ── */
const macro = computed<MacroLiquiditySummary | null>(
  () =>
    (run.value?.summary as { macro_liquidity?: MacroLiquiditySummary } | null | undefined)
      ?.macro_liquidity ?? null,
);
const macroUnavailable = computed(() => macro.value?.status === 'unavailable');

const MACRO_LEVEL_TEXT: Record<number, string> = {
  2: '宽松',
  1: '偏松',
  0: '中性',
  [-1]: '偏紧',
  [-2]: '深度紧缩',
};

function macroLevelText(lv: number | null | undefined): string {
  return MACRO_LEVEL_TEXT[lv ?? 0] ?? '—';
}

function macroTone(lv: number | null | undefined): BadgeTone {
  if (lv == null) return 'default';
  if (lv >= 1) return 'success';
  if (lv <= -1) return 'warn';
  return 'info';
}

function fmtSigned(v?: number | null): string {
  if (v == null || !Number.isFinite(v)) return DASH;
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}`;
}

/* ── 下一交易日指引 ────────────────────────────── */
const actions = computed<string[]>(() => {
  const set = new Set<string>();
  for (const s of review.value?.signals ?? []) {
    if (s.action) set.add(String(s.action));
  }
  return [...set];
});

const holidayText = computed(() =>
  review.value?.holiday_days == null ? DASH : String(review.value.holiday_days),
);

/* ── 数据截至 / 口径 ───────────────────────────── */
const asOfText = computed(() =>
  fmtDate(
    review.value?.as_of ??
      review.value?.data_quality?.last_bar ??
      run.value?.as_of ??
      pf.value?.latest_run?.as_of,
  ),
);
const methodText = computed(() => review.value?.portfolio_metrics?.method ?? DASH);

/* ── 加载 ─────────────────────────────────────── */
async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const portfolio = await getPortfolio(pfId.value);
    pf.value = portfolio;
    let runs: Run[] = [];
    try {
      // runs 过滤按数值主键（pid 为 'cn4' 等短码，Number(pid) 会得到 NaN → 422）
      runs = await listRuns({ type: 'review', portfolio_id: portfolio.id, limit: 1 });
    } catch {
      runs = [];
    }
    run.value = runs[0] ?? null;
    review.value = null;
    // 数据风险随 run 持久化（runs.summary.data_risk），刷新后仍可见
    const summary = run.value?.summary as { data_risk?: DataRisk | null } | null | undefined;
    dataRisk.value = summary?.data_risk ?? null;
    if (run.value) {
      try {
        review.value = await fetchRunArtifact<ReviewPayload>(run.value.run_uid, 'review.json', 'json');
      } catch {
        review.value = null; // 404 容错：产物尚未生成/不可读
      }
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '组合详情加载失败';
  } finally {
    loading.value = false;
  }
}

/* ── 评估 + 轮询 ───────────────────────────────── */
/** 数据风险提示条未通过项文案（无明细时兜底）。 */
const riskFailedText = computed(() => {
  const risk = dataRisk.value;
  const failed = risk?.failed ?? [];
  const names = failed.map((f) => f.check).filter(Boolean);
  if (names.length) return names.join('、');
  // unknown（从未验证）与 fail（有检查未通过）语义不同，回退文案需区分
  return risk?.status === 'unknown' ? '验证状态未知' : '存在未通过项';
});

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollUntilDone(jobId: string): Promise<void> {
  for (let i = 0; i < 120; i += 1) {
    const job = await getJob(jobId);
    progress.value = typeof job.progress === 'number' ? job.progress : 0;
    if (job.status === 'done') return;
    if (job.status === 'failed') {
      throw new ApiError('JOB_FAILED', job.error || '评估任务失败', 500);
    }
    await sleep(1000); // 轮询间隔 ≥1s（β 不做 SSE）
  }
  throw new ApiError('JOB_TIMEOUT', '评估任务超时，请稍后在运行历史查看', 504);
}

function handleEvalError(err: unknown): void {
  if (err instanceof ApiError) {
    if (err.code === 'JOB_LOCKED') {
      queued.value = true;
      toast.info('同 workspace 有计算任务进行中，评估已排队，请稍后重试');
      return;
    }
    toast.error(err.message);
    return;
  }
  toast.error(err instanceof Error ? err.message : '评估失败');
}

async function evaluate(): Promise<void> {
  if (evaluating.value) return;
  evaluating.value = true;
  dataRisk.value = null;
  queued.value = false;
  progress.value = 0;
  try {
    const { job_id, data_risk } = await reviewPortfolio(pfId.value);
    // 门禁不阻断评估：非全绿仅提示数据风险
    if (data_risk) {
      dataRisk.value = data_risk;
      toast.warn(data_risk.message);
    }
    await pollUntilDone(job_id);
    await load();
    toast.success('评估完成');
  } catch (err) {
    handleEvalError(err);
  } finally {
    evaluating.value = false;
  }
}

/* ── 持仓数据手动拉取 ─────────────────────────── */
const syncingSec = ref(false);

/** 拉取组合持仓标的数据（逐只 refresh，近 5 日窗口增量合并）。
 * sync 全局互斥：任一持仓已在同步 → 本组合其余持仓顺延提示（用户稍后重试即可）。
 */
async function syncPositions(): Promise<void> {
  const positions = pf.value?.positions ?? [];
  if (syncingSec.value || positions.length === 0) return;
  syncingSec.value = true;
  try {
    const { job_id } = await syncSecid(positions[0].secid);
    progress.value = 0;
    await pollUntilDone(job_id);
    toast.success(`持仓数据已更新（首只 ${positions[0].secid}；其余可再点或到数据页增量同步）`);
    await load();
  } catch (err) {
    if (err instanceof ApiError && err.code === 'JOB_LOCKED') {
      toast.info('已有同步任务在运行，完成后数据自动覆盖；稍后可再拉其余持仓');
    } else {
      toast.error(err instanceof Error ? err.message : '拉取失败');
    }
  } finally {
    syncingSec.value = false;
  }
}

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row pd-head">
      <div>
        <h1 class="ap-title">{{ pf?.name ?? `组合详情 · ${pfId}` }}</h1>
        <p class="ap-page__desc ap-row pd-meta">
          <ApBadge tone="info" data-test="asof-badge">数据截至 {{ asOfText }}</ApBadge>
          <span class="ap-muted" data-test="method">口径：{{ methodText }}</span>
        </p>
      </div>
      <div class="ap-row">
        <ApButton
          variant="ghost"
          :loading="syncingSec"
          :disabled="loading || !pf?.positions?.length"
          data-test="sync-positions-btn"
          @click="syncPositions"
        >
          拉取持仓数据
        </ApButton>
        <RouterLink :to="`/portfolios/${pfId}/report`">
          <ApButton variant="ghost" data-test="report-nav">查看报告</ApButton>
        </RouterLink>
        <ApButton :loading="evaluating" :disabled="loading" data-test="evaluate-btn" @click="evaluate">
          评估
        </ApButton>
      </div>
    </div>

    <div v-if="dataRisk" class="pd-risk" role="alert" data-test="risk-bar">
      <span class="pd-risk__txt" data-test="risk-text">
        {{ dataRisk.message }}（{{ riskFailedText }}）
      </span>
      <RouterLink class="pd-risk__link" to="/data" data-test="risk-link">前往数据页查看明细</RouterLink>
    </div>
    <p v-if="queued" class="ap-muted pd-note" data-test="queued-hint">
      同 workspace 有计算任务进行中，评估已排队，请稍后重试。
    </p>
    <p v-if="evaluating" class="ap-muted pd-note" data-test="eval-progress">评估中… {{ progress }}%</p>

    <p v-if="loading" class="ap-muted" data-test="detail-loading">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p class="pd-error" role="alert" data-test="detail-error">{{ error }}</p>
      <ApButton variant="ghost" data-test="detail-retry" @click="load">重试</ApButton>
    </ApCard>

    <ApCard v-else-if="!run" title="概览">
      <p class="ap-muted" data-test="empty-state">该组合尚无评估记录，请先运行评估生成概览与报告。</p>
      <ApButton class="pd-mt" :loading="evaluating" data-test="empty-evaluate" @click="evaluate">
        先运行评估
      </ApButton>
    </ApCard>

    <ApCard v-else title="概览">
      <ApTabs v-model="active" :tabs="tabs">
        <template #default="{ active: current }">
          <div v-if="current === 'overview'" class="ap-stack">
            <p v-if="!review" class="ap-muted" data-test="review-missing">评估产物缺失或不可读。</p>

            <ApCard title="体检卡" flat>
              <div class="pd-metrics">
                <div v-for="m in metrics" :key="m.key" class="pd-metric">
                  <span class="pd-metric__lbl">{{ m.label }}</span>
                  <span
                    class="ap-mono pd-metric__val"
                    :class="m.tone"
                    :data-test="`metric-${m.key}`"
                  >
                    {{ m.value }}
                  </span>
                </div>
              </div>
            </ApCard>

            <ApCard title="引擎A · 因子">
              <template #extra>
                <span v-if="usedPeriods.length" data-test="a-stale">
                  口径周期：{{ usedPeriods.join(' / ') }}
                </span>
              </template>
              <div v-if="isMobile" class="fa-cards" data-test="fa-cards">
                <div v-for="row in aRows" :key="String(row.secid)" class="fa-card">
                  <div class="fa-card__head ap-mono">{{ row.name }}</div>
                  <div v-for="col in aColumns" :key="col.key" class="fa-card__row">
                    <span class="pd-metric__lbl">{{ col.label }}</span>
                    <span class="ap-mono">{{ row[col.key] }}</span>
                  </div>
                </div>
              </div>
              <ApTable
                v-else
                :columns="aColumns"
                :rows="aRows"
                row-key="secid"
                density="compact"
                data-test="fa-table"
              >
                <template #cell:name="{ row }">
                  <ApMarketBadge
                    :market="secMeta.get(String(row.secid))?.market"
                    :hk-connect="secMeta.get(String(row.secid))?.hk_connect"
                  />
                  <span :data-test="`fa-name-${String(row.secid)}`">{{ row.name }}</span>
                </template>
                <template #cell:pe_ttm="{ row }">
                  <span class="ap-mono" :data-test="`fa-pe-${String(row.secid)}`">{{ row.pe_ttm }}</span>
                </template>
              </ApTable>
            </ApCard>

            <ApCard title="引擎B · 20日信号">
              <div v-if="positions.length" class="bd-grid">
                <div v-for="p in positions" :key="p.secid" class="bd-card" data-test="beta-card">
                  <div class="bd-head">
                    <span class="ap-row" style="gap: 4px">
                      <ApMarketBadge
                        :market="secMeta.get(p.secid)?.market"
                        :hk-connect="secMeta.get(p.secid)?.hk_connect"
                      />
                      <span class="ap-mono">{{ p.name || p.secid }}</span>
                    </span>
                    <ApBadge :tone="stateTone(p.beta?.state)" data-test="beta-state">
                      {{ p.beta?.state ?? DASH }}
                    </ApBadge>
                  </div>
                  <div class="bd-rows">
                    <span>20日动量 <b class="ap-mono">{{ fmtPct(p.beta?.mom_20d) }}</b></span>
                    <span>跌破MA20 <b class="ap-mono">{{ boolText(p.beta?.ma20_break) }}</b></span>
                    <span>触及20日高 <b class="ap-mono">{{ boolText(p.beta?.high20_touch) }}</b></span>
                  </div>
                  <ApBadge v-if="hasCooldown(p.secid)" tone="warn" data-test="cooldown">冷却中</ApBadge>
                </div>
              </div>
              <p v-else class="ap-muted">暂无持仓信号。</p>
            </ApCard>

            <ApCard title="引擎C · 宏观流动性">
              <p v-if="macroUnavailable" class="ap-muted" data-test="macro-unavailable">
                宏观流动性数据未同步（不影响其余评估）
              </p>
              <template v-else-if="macro">
                <div class="ap-row macro-head">
                  <ApBadge
                    v-for="(lv, mkt) in macro.markets"
                    :key="mkt"
                    :tone="macroTone(lv)"
                    :data-test="`macro-badge-${mkt}`"
                  >
                    {{ mkt }}市场 {{ macroLevelText(lv) }} ({{ lv > 0 ? '+' : '' }}{{ lv }})
                  </ApBadge>
                  <span class="ap-mono" data-test="macro-global">
                    全局 {{ macroLevelText(macro.global ?? 0) }} {{ fmtSigned(macro.global_score) }}
                  </span>
                  <span class="ap-muted">截至 {{ fmtDate(macro.as_of) }}</span>
                </div>
                <p v-if="macro.state === 'tight_stress'" class="macro-warn" data-test="macro-warn">
                  流动性紧缩，风险资产承压概率升高
                </p>
                <p v-else-if="macro.state === 'easy_froth'" class="macro-warn" data-test="macro-warn">
                  流动性充裕，注意拥挤度与退出风险
                </p>
                <p class="ap-muted macro-note">市场级条件变量，不构成买卖信号</p>
              </template>
              <p v-else class="ap-muted">暂无宏观数据</p>
            </ApCard>

            <ApCard title="下一交易日指引">
              <div class="ap-row nt-head">
                <span class="ap-kicker">下一交易日</span>
                <span class="ap-mono" data-test="next-trade-date">{{ fmtDate(review?.next_trade_date) }}</span>
                <span class="ap-muted">休市 <b class="ap-mono" data-test="holiday-days">{{ holidayText }}</b> 天</span>
              </div>
              <ul v-if="actions.length" class="nt-actions" data-test="actions">
                <li v-for="(a, i) in actions" :key="i">
                  <ApBadge tone="info">操作</ApBadge> <span>{{ a }}</span>
                </li>
              </ul>
              <p v-else class="ap-muted" data-test="no-action">无操作</p>
            </ApCard>
          </div>

          <div v-else class="ap-stack">
            <p class="ap-muted">报告页提供 HTML 全文嵌入与 MD/原文下载。</p>
            <RouterLink :to="`/portfolios/${pfId}/report`">
              <ApButton variant="ghost" data-test="report-link">打开报告页</ApButton>
            </RouterLink>
          </div>
        </template>
      </ApTabs>
    </ApCard>
  </main>
</template>

<style scoped>
.pd-head {
  justify-content: space-between;
  align-items: flex-start;
}
.pd-meta {
  flex-wrap: wrap;
}
.pd-risk {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  margin-bottom: var(--ap-sp-4);
  padding: 10px 13px;
  border: 1px solid rgba(176, 140, 60, 0.45);
  background: rgba(176, 140, 60, 0.08);
  border-radius: var(--ap-radius);
  color: var(--ap-warn);
  font-size: var(--ap-fs-sm);
}
.pd-risk__link {
  color: var(--ap-warn);
  font-weight: 600;
  text-decoration: underline;
}
/* 引擎C 极端态提示：复用 .pd-risk 琥珀配色（单行缩简） */
.macro-warn {
  margin-top: var(--ap-sp-2);
  padding: 6px 10px;
  border: 1px solid rgba(176, 140, 60, 0.45);
  background: rgba(176, 140, 60, 0.08);
  border-radius: var(--ap-radius);
  color: var(--ap-warn);
  font-size: var(--ap-fs-sm);
}
.macro-head {
  flex-wrap: wrap;
  gap: var(--ap-sp-2);
}
.macro-note {
  margin-top: var(--ap-sp-2);
}
.pd-note {
  margin-bottom: var(--ap-sp-3);
}
.pd-error {
  color: var(--ap-danger);
  margin-bottom: var(--ap-sp-3);
}
.pd-mt {
  margin-top: var(--ap-sp-3);
}
.pd-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--ap-sp-3);
}
.pd-metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.pd-metric__lbl {
  font-family: var(--ap-font-mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--ap-mute);
}
.pd-metric__val {
  font-size: var(--ap-fs-md);
}
.pd-metric__val.is-up {
  color: var(--ap-up);
}
.pd-metric__val.is-down {
  color: var(--ap-down);
}
.bd-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--ap-sp-3);
}
.bd-card {
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
  padding: var(--ap-sp-3);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  background: var(--ap-card2);
}
.bd-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-sp-2);
}
.bd-rows {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
.nt-head {
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
}
.nt-actions {
  margin: var(--ap-sp-3) 0 0;
  padding-left: var(--ap-sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
  font-size: var(--ap-fs-sm);
}
.fa-cards {
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-3);
}
.fa-card {
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  background: var(--ap-card2);
  padding: var(--ap-sp-3);
}
.fa-card__head {
  font-size: var(--ap-fs-md);
  font-weight: 600;
  margin-bottom: var(--ap-sp-2);
}
.fa-card__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-sp-2);
  padding: 3px 0;
  border-bottom: 1px solid var(--ap-hair);
  font-size: var(--ap-fs-sm);
}
.fa-card__row:last-child {
  border-bottom: 0;
}
@media (max-width: 767px) {
  .pd-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
