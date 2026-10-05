<script setup lang="ts">
// 回测页（T6.5）：参数表单（区间/再平衡/基准/rf）→ 提交 job 轮询 → 净值双线（ECharts）
// + 八指标对比卡 + 年度月度收益表 + 标的贡献表 + 历史回测列表。数据源为最近一次 backtest run
// 的 backtest.json 产物（runs.summary 仅有标量指标，无 series）。
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import {
  backtestPortfolio,
  fetchRunArtifact,
  getDataStatus,
  getJob,
  getPortfolio,
  listRuns,
} from '../api';
import { ApiError } from '../api/types';
import type {
  BacktestMetrics,
  BacktestPayload,
  BacktestRequest,
  Portfolio,
  Run,
} from '../api/types';
import { ApButton, ApCard, ApTable, useToast } from '../components/ui';
import type { ApTableColumn } from '../components/ui';
import echarts from '../utils/echarts';
import type { ECharts } from '../utils/echarts';
import { DASH, fmtDate, fmtNum, fmtPct } from '../utils/format';

const route = useRoute();
const toast = useToast();

const pfId = computed(() => String(route.params.id));

/* ── 状态 ─────────────────────────────────────── */
const pf = ref<Portfolio | null>(null);
const runs = ref<Run[]>([]);
const selectedUid = ref<string | null>(null);
const payload = ref<BacktestPayload | null>(null);
const loading = ref(false);
const error = ref('');
const running = ref(false);
const progress = ref(0);
/** 门禁失败项（409 VERIFY_GATE_FAILED 的 detail.failed）；非空即渲染红条。 */
const gateFailed = ref<string[] | null>(null);

const form = reactive({
  start: '',
  end: '',
  rebalance: 'monthly',
  benchmark_secid: '510300.SH',
  rf: 0,
});

/** 基准下拉：默认恒含 510300.SH（沪深300ETF），再并入 data/status 的 universe（失败则仅默认）。 */
const benchOptions = ref<{ secid: string; name: string }[]>([
  { secid: '510300.SH', name: '沪深300ETF' },
]);

const REBALANCE_LABELS: { value: string; label: string }[] = [
  { value: 'daily', label: '每日' },
  { value: 'monthly', label: '月度' },
  { value: 'quarterly', label: '季度' },
  { value: 'yearly', label: '年度' },
  { value: 'buyhold', label: '买入持有' },
];

/* ── 加载 ─────────────────────────────────────── */
async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const portfolio = await getPortfolio(pfId.value);
    pf.value = portfolio;
    let list: Run[] = [];
    try {
      // runs 过滤按数值主键（pid 为 'cn4' 等短码，Number(pid) 会得到 NaN → 422）
      list = await listRuns({ type: 'backtest', portfolio_id: portfolio.id, limit: 20 });
    } catch {
      list = [];
    }
    runs.value = list;
    const newest = list[0] ?? null;
    if (newest) {
      await selectRun(newest);
    } else {
      selectedUid.value = null;
      payload.value = null;
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '回测页加载失败';
  } finally {
    loading.value = false;
  }
}

/** 选中某次回测并拉取其 backtest.json 产物（404/不可读容错 → payload 置空）。 */
async function selectRun(run: Run): Promise<void> {
  selectedUid.value = run.run_uid;
  payload.value = null;
  try {
    payload.value = await fetchRunArtifact<BacktestPayload>(run.run_uid, 'backtest.json', 'json');
  } catch {
    payload.value = null;
  }
}

/** 基准下拉：尝试并入 data/status 的 universe，失败保持仅 510300.SH。 */
async function loadBenchmarks(): Promise<void> {
  try {
    const status = await getDataStatus();
    const map = new Map<string, string>([['510300.SH', '沪深300ETF']]);
    for (const s of status.securities ?? []) {
      if (s.secid && s.secid !== '510300.SH') map.set(s.secid, s.name || s.secid);
    }
    benchOptions.value = [...map.entries()].map(([secid, name]) => ({ secid, name }));
  } catch {
    // 忽略：保留默认 510300.SH，不阻塞页面
  }
}

/* ── 运行 + 轮询 ───────────────────────────────── */
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
      throw new ApiError('JOB_FAILED', job.error || '回测任务失败', 500);
    }
    await sleep(1000); // 轮询间隔 ≥1s（β 不做 SSE）
  }
  throw new ApiError('JOB_TIMEOUT', '回测任务超时，请稍后在历史回测查看', 504);
}

function handleRunError(err: unknown): void {
  if (err instanceof ApiError) {
    if (err.code === 'VERIFY_GATE_FAILED') {
      gateFailed.value = extractFailed(err.detail);
      toast.error('数据验证未全绿，回测被门禁拦截');
      return;
    }
    toast.error(err.message);
    return;
  }
  toast.error(err instanceof Error ? err.message : '回测失败');
}

async function runBacktest(): Promise<void> {
  if (running.value) return;
  running.value = true;
  gateFailed.value = null;
  progress.value = 0;
  try {
    const body: BacktestRequest = {
      rebalance: form.rebalance,
      rf: form.rf,
      benchmark_secid: form.benchmark_secid,
    };
    if (form.start) body.start = form.start;
    if (form.end) body.end = form.end;
    const { job_id } = await backtestPortfolio(pfId.value, body);
    await pollUntilDone(job_id);
    await load(); // 重载 runs 并自动选中最新的
    toast.success('回测完成');
  } catch (err) {
    handleRunError(err);
  } finally {
    running.value = false;
  }
}

/* ── 绩效总览（八指标对比） ───────────────────── */
type MetricKey =
  | 'total_return'
  | 'cagr'
  | 'ann_vol'
  | 'max_dd'
  | 'sharpe'
  | 'sortino'
  | 'calmar'
  | 'win_rate';

function metricVal(m: BacktestMetrics | null | undefined, k: MetricKey): number | null | undefined {
  return m ? m[k] : undefined;
}

function excessOf(k: MetricKey): string {
  const a = metricVal(payload.value?.strategy_metrics, k);
  const b = metricVal(payload.value?.benchmark?.metrics, k);
  if (a == null || b == null) return DASH;
  return fmtPct(a - b);
}

const metricColumns: ApTableColumn[] = [
  { key: 'metric', label: '指标' },
  { key: 'strategy', label: '组合', align: 'right' },
  { key: 'benchmark', label: '基准', align: 'right' },
  { key: 'excess', label: '超额', align: 'right' },
];

const metricRows = computed(() => {
  const sm = payload.value?.strategy_metrics;
  const bm = payload.value?.benchmark?.metrics;
  const defs: { code: MetricKey; metric: string; fmt: typeof fmtPct }[] = [
    { code: 'total_return', metric: '累计收益', fmt: fmtPct },
    { code: 'cagr', metric: '年化收益(CAGR)', fmt: fmtPct },
    { code: 'ann_vol', metric: '年化波动', fmt: fmtPct },
    { code: 'max_dd', metric: '最大回撤', fmt: fmtPct },
    { code: 'sharpe', metric: '夏普', fmt: fmtNum },
    { code: 'sortino', metric: '索提诺', fmt: fmtNum },
    { code: 'calmar', metric: '卡玛', fmt: fmtNum },
    { code: 'win_rate', metric: '日胜率', fmt: fmtPct },
  ];
  return defs.map((d) => ({
    code: d.code,
    metric: d.metric,
    strategy: d.fmt(metricVal(sm, d.code)),
    benchmark: d.fmt(metricVal(bm, d.code)),
    excess: excessOf(d.code),
  }));
});

/* ── 月度收益表 ───────────────────────────────── */
const monthKeys = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(2, '0'));

const monthlyColumns: ApTableColumn[] = [
  { key: 'year', label: '年份' },
  ...monthKeys.map((m) => ({ key: m, label: m, align: 'right' as const })),
  { key: 'Y', label: '全年', align: 'right' },
];

const monthlyRows = computed<Record<string, unknown>[]>(() => {
  const yearly = payload.value?.yearly ?? {};
  return Object.keys(yearly)
    .sort()
    .map((year) => {
      const row: Record<string, unknown> = { year };
      for (const m of monthKeys) row[m] = fmtPct(yearly[year]?.[m]);
      row.Y = fmtPct(yearly[year]?.Y);
      return row;
    });
});

/* ── 标的贡献表 ───────────────────────────────── */
const assetColumns: ApTableColumn[] = [
  { key: 'name', label: '标的' },
  { key: 'total_return', label: '区间累计', align: 'right' },
  { key: 'cagr', label: '年化收益', align: 'right' },
  { key: 'ann_vol', label: '年化波动', align: 'right' },
  { key: 'max_dd', label: '最大回撤', align: 'right' },
  { key: 'corr', label: '与组合相关性', align: 'right' },
];

const assetRows = computed<Record<string, unknown>[]>(() =>
  (payload.value?.per_asset ?? []).map((a) => ({
    secid: a.secid,
    name: `${a.name} ${a.secid}`,
    total_return: fmtPct(a.total_return),
    cagr: fmtPct(a.cagr),
    ann_vol: fmtPct(a.ann_vol),
    max_dd: fmtPct(a.max_dd),
    corr: fmtNum(a.corr),
  })),
);

/* ── 净值曲线（ECharts 双线） ─────────────────── */
const chartEl = ref<HTMLDivElement | null>(null);
let chart: ECharts | null = null;

function renderChart(): void {
  const el = chartEl.value;
  const p = payload.value;
  if (!el || !p || !p.series || p.series.length === 0) return;
  try {
    if (!chart) chart = echarts.init(el);
    const series: Record<string, unknown>[] = [
      {
        name: '组合净值',
        type: 'line',
        showSymbol: false,
        data: p.series,
        lineStyle: { color: '#a93226', width: 2 },
        itemStyle: { color: '#a93226' },
      },
    ];
    if (p.benchmark?.series?.length) {
      series.push({
        name: `${p.benchmark.name}（买入持有）`,
        type: 'line',
        showSymbol: false,
        data: p.benchmark.series,
        lineStyle: { color: '#9c9585', width: 1.5, type: 'dashed' },
        itemStyle: { color: '#9c9585' },
      });
    }
    chart.setOption(
      {
        grid: { left: 52, right: 18, top: 34, bottom: 30 },
        tooltip: { trigger: 'axis' },
        legend: { data: series.map((s) => s.name), top: 0 },
        xAxis: { type: 'category', boundaryGap: false, data: p.series.map((d) => d[0]) },
        yAxis: { type: 'value', scale: true, axisLabel: { formatter: '{value}' } },
        series,
      },
      true,
    );
  } catch {
    // jsdom/零尺寸下 init 可能失败：图表属增强，不应影响页面其它部分渲染
  }
}

function onResize(): void {
  try {
    chart?.resize();
  } catch {
    // 忽略 resize 异常
  }
}

watch(payload, async () => {
  await nextTick();
  renderChart();
});

onMounted(() => {
  window.addEventListener('resize', onResize);
  loadBenchmarks();
  load();
});

onUnmounted(() => {
  window.removeEventListener('resize', onResize);
  try {
    chart?.dispose();
  } catch {
    // 忽略 dispose 异常
  }
  chart = null;
});
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head">
      <h1 class="ap-title">回测 · {{ pf?.name ?? pfId }}</h1>
      <p class="ap-page__desc">等权组合回测：净值双线、绩效对比、月度收益与标的贡献。</p>
    </div>

    <div v-if="gateFailed" class="bt-gate" role="alert" data-test="bt-gate">
      <span class="bt-gate__txt">
        数据验证未全绿，回测被门禁拦截：{{ gateFailed.length ? gateFailed.join('、') : '存在未通过项' }}
      </span>
      <RouterLink class="bt-gate__link" to="/data" data-test="bt-gate-link">前往数据页查看明细</RouterLink>
    </div>
    <p v-if="running" class="ap-muted bt-note" data-test="bt-progress">运行中… {{ progress }}%</p>

    <p v-if="loading" class="ap-muted" data-test="bt-loading">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p class="bt-error" role="alert" data-test="bt-error">{{ error }}</p>
      <ApButton variant="ghost" data-test="bt-retry" @click="load">重试</ApButton>
    </ApCard>

    <div v-else class="ap-stack">
      <ApCard title="参数">
        <div class="bt-form">
          <label class="bt-field">
            <span class="bt-field__lbl">起始日期</span>
            <input v-model="form.start" type="date" class="bt-input" data-test="bt-start" />
          </label>
          <label class="bt-field">
            <span class="bt-field__lbl">结束日期</span>
            <input v-model="form.end" type="date" class="bt-input" data-test="bt-end" />
          </label>
          <label class="bt-field">
            <span class="bt-field__lbl">再平衡</span>
            <select v-model="form.rebalance" class="bt-input" data-test="bt-rebalance">
              <option v-for="o in REBALANCE_LABELS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </label>
          <label class="bt-field">
            <span class="bt-field__lbl">基准</span>
            <select v-model="form.benchmark_secid" class="bt-input" data-test="bt-benchmark">
              <option v-for="o in benchOptions" :key="o.secid" :value="o.secid">
                {{ o.name }} ({{ o.secid }})
              </option>
            </select>
          </label>
          <label class="bt-field bt-field--rf">
            <span class="bt-field__lbl">无风险利率 rf</span>
            <input
              v-model.number="form.rf"
              type="number"
              step="0.01"
              class="bt-input"
              data-test="bt-rf"
            />
          </label>
          <div class="bt-form__actions">
            <ApButton :loading="running" data-test="bt-run" @click="runBacktest">运行回测</ApButton>
          </div>
        </div>
      </ApCard>

      <ApCard v-if="runs.length" title="历史回测">
        <div class="bt-history" data-test="bt-history">
          <button
            v-for="run in runs"
            :key="run.run_uid"
            type="button"
            class="bt-history__item"
            :class="{ 'is-active': run.run_uid === selectedUid }"
            data-test="bt-history-item"
            @click="selectRun(run)"
          >
            <span class="bt-history__date ap-mono">{{ fmtDate(run.as_of) }}</span>
            <span class="bt-history__uid ap-mono">{{ run.run_uid.slice(0, 10) }}</span>
            <span class="bt-history__rebal ap-muted">
              再平衡 {{ Number(run.summary?.n_rebalances ?? 0) }} 次
            </span>
          </button>
        </div>
      </ApCard>

      <ApCard v-else title="历史回测">
        <div data-test="bt-empty">
          <p class="ap-muted bt-note">尚无回测记录，可立即运行一次回测。</p>
          <ApButton :loading="running" data-test="bt-empty-run" @click="runBacktest">
            立即回测
          </ApButton>
        </div>
      </ApCard>

      <template v-if="payload">
        <ApCard title="净值曲线">
          <div ref="chartEl" class="bt-chart" data-test="bt-chart" />
        </ApCard>

        <ApCard title="绩效总览">
          <p class="bt-meta">
            <span>区间 {{ fmtDate(payload.start) }} ~ {{ fmtDate(payload.end) }}</span>
            <span>交易日 <b class="ap-mono" data-test="bt-n-days">{{ payload.n_days }}</b></span>
            <span>再平衡 <b class="ap-mono" data-test="bt-n-rebalances">{{ payload.n_rebalances }}</b> 次</span>
            <span class="ap-muted">{{ payload.strategy }}</span>
          </p>
          <ApTable
            :columns="metricColumns"
            :rows="metricRows"
            row-key="code"
            density="compact"
            data-test="bt-metrics-table"
          >
            <template #cell:strategy="{ row }">
              <span
                v-if="row.code === 'total_return'"
                class="ap-mono"
                data-test="bt-total-return"
              >{{ row.strategy }}</span>
              <span v-else class="ap-mono">{{ row.strategy }}</span>
            </template>
          </ApTable>
        </ApCard>

        <ApCard title="月度收益">
          <ApTable
            :columns="monthlyColumns"
            :rows="monthlyRows"
            row-key="year"
            density="compact"
            empty-text="暂无月度数据"
            data-test="bt-monthly"
          />
        </ApCard>

        <ApCard title="标的贡献">
          <ApTable
            :columns="assetColumns"
            :rows="assetRows"
            row-key="secid"
            density="compact"
            data-test="bt-per-asset"
          />
        </ApCard>
      </template>
    </div>
  </main>
</template>

<style scoped>
.bt-gate {
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
.bt-gate__link {
  color: var(--ap-seal);
  font-weight: 600;
  text-decoration: underline;
}
.bt-note {
  margin-bottom: var(--ap-sp-3);
}
.bt-error {
  color: var(--ap-danger);
  margin-bottom: var(--ap-sp-3);
}
.bt-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--ap-sp-3);
  align-items: end;
}
.bt-field {
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-1);
}
.bt-field__lbl {
  font-family: var(--ap-font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--ap-mute);
}
.bt-input {
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  background: var(--ap-card);
  color: var(--ap-ink);
  font-family: var(--ap-font-sans);
  font-size: var(--ap-fs-sm);
}
.bt-input:focus {
  outline: none;
  border-color: var(--ap-seal);
}
.bt-form__actions {
  display: flex;
  align-items: flex-end;
}
.bt-history {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--ap-sp-2);
}
.bt-history__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-sp-2);
  padding: 8px 11px;
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  background: var(--ap-card2);
  color: var(--ap-ink);
  cursor: pointer;
  text-align: left;
  font-size: var(--ap-fs-sm);
  transition: 0.2s;
}
.bt-history__item:hover {
  border-color: var(--ap-seal);
}
.bt-history__item.is-active {
  border-color: var(--ap-seal);
  background: var(--ap-seal-soft);
}
.bt-history__date {
  font-weight: 600;
}
.bt-history__rebal {
  font-size: var(--ap-fs-xs);
}
.bt-chart {
  width: 100%;
  height: 300px;
}
.bt-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--ap-sp-3);
  margin-bottom: var(--ap-sp-3);
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
@media (max-width: 767px) {
  .bt-form {
    grid-template-columns: 1fr;
  }
  .bt-history {
    grid-template-columns: 1fr;
  }
  .bt-chart {
    height: 220px;
  }
}
</style>
