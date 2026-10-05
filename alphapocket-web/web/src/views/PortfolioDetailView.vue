<script setup lang="ts">
// 组合详情（T6.4）：Tab 概览（体检卡五指标 / 引擎A 因子表 / 引擎B 信号卡 / 下一交易日指引
// / 数据截至徽章 + 门禁红条）+ Tab 报告入口。数据源为最近一次 review run 的 review.json 产物。
import { computed, onMounted, ref } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import { fetchRunArtifact, getJob, getPortfolio, listRuns, reviewPortfolio } from '../api';
import { ApiError } from '../api/types';
import type { Portfolio, ReviewPayload, ReviewPosition, Run } from '../api/types';
import { ApBadge, ApButton, ApCard, ApTable, ApTabs, useToast } from '../components/ui';
import type { ApTableColumn, ApTabItem, BadgeTone } from '../components/ui';
import { useBreakpoint } from '../composables/useBreakpoint';
import { DASH, fmtDate, fmtNum, fmtPct } from '../utils/format';

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
/** 门禁失败项（409 VERIFY_GATE_FAILED 的 detail.failed）；非空即渲染红条。 */
const gateFailed = ref<string[] | null>(null);
const queued = ref(false);

const positions = computed<ReviewPosition[]>(() => review.value?.positions ?? []);

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
      roe_ttm: fmtPct(a?.qua_roe_ttm),
      margin: fmtPct(a?.qua_margin),
      rev_yoy: fmtPct(a?.grow_rev_yoy),
      np_yoy: fmtPct(a?.grow_np_yoy),
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
function extractFailed(detail: unknown): string[] {
  if (!detail || typeof detail !== 'object') return [];
  const d = detail as Record<string, unknown>;
  const f = d.failed ?? d.failures;
  if (Array.isArray(f)) {
    return f.map((x) => {
      if (typeof x === 'string') return x;
      if (x && typeof x === 'object') {
        const o = x as Record<string, unknown>;
        return String(o.check ?? o.name ?? o.code ?? JSON.stringify(x));
      }
      return String(x);
    });
  }
  if (typeof f === 'string' && f) return [f];
  return [];
}

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
    if (err.code === 'VERIFY_GATE_FAILED') {
      gateFailed.value = extractFailed(err.detail);
      toast.error('数据验证未全绿，评估被门禁拦截');
      return;
    }
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
  gateFailed.value = null;
  queued.value = false;
  progress.value = 0;
  try {
    const { job_id } = await reviewPortfolio(pfId.value);
    await pollUntilDone(job_id);
    await load();
    toast.success('评估完成');
  } catch (err) {
    handleEvalError(err);
  } finally {
    evaluating.value = false;
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
        <RouterLink :to="`/portfolios/${pfId}/report`">
          <ApButton variant="ghost" data-test="report-nav">查看报告</ApButton>
        </RouterLink>
        <ApButton :loading="evaluating" :disabled="loading" data-test="evaluate-btn" @click="evaluate">
          评估
        </ApButton>
      </div>
    </div>

    <div v-if="gateFailed" class="pd-gate" role="alert" data-test="gate-bar">
      <span class="pd-gate__txt">
        数据验证未全绿，评估被门禁拦截：{{ gateFailed.length ? gateFailed.join('、') : '存在未通过项' }}
      </span>
      <RouterLink class="pd-gate__link" to="/data" data-test="gate-link">前往数据页查看明细</RouterLink>
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
                    <span class="ap-mono">{{ p.name || p.secid }}</span>
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
.pd-gate {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  margin-bottom: var(--ap-sp-4);
  padding: 10px 13px;
  border: 1px solid var(--ap-seal-line);
  background: var(--ap-seal-soft);
  border-radius: var(--ap-radius);
  color: var(--ap-danger);
  font-size: var(--ap-fs-sm);
}
.pd-gate__link {
  color: var(--ap-seal);
  font-weight: 600;
  text-decoration: underline;
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
