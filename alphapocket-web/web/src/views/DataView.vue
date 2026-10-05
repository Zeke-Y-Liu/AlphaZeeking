<script setup lang="ts">
// 数据页（T6.7）：universe 状态表（最后日期/缺口/豁免与缺失分行/类型徽章）
// + 六项验证徽章墙（点击 → 抽屉看 detail）+ 手动 sync（bootstrap 二次确认）
// + job 进度条（轮询）+ /runs 运行历史（筛选 + 产物直达下载）。
import { computed, onMounted, ref } from 'vue';

import { fetchRunArtifact, getDataStatus, getJob, listRuns, listVerifications, syncData } from '../api';
import { ApiError } from '../api/types';
import type { DataStatus, Run, SyncMode, VerifyDetail, VerifyResult, VerifyStatus } from '../api/types';
import { ApBadge, ApButton, ApCard, ApModal, ApTable, useToast } from '../components/ui';
import type { ApTableColumn, BadgeTone } from '../components/ui';
import { DASH, fmtDate } from '../utils/format';

const toast = useToast();

/** 六项验证（冻结顺序，与 services.verification.CHECKS 一致）。 */
const CHECKS = [
  'pit_audit',
  'golden_test',
  'cross_source',
  'calendar_check',
  'adjust_check',
  'provenance_check',
] as const;

const RUN_TYPES = ['', 'data_sync', 'review', 'backtest', 'rebalance_plan', 'exec_orders'] as const;

/* ── 状态 ───────────────────────────────────────── */
const status = ref<DataStatus | null>(null);
const verifications = ref<VerifyResult[]>([]);
const loading = ref(true);
const error = ref('');

const syncing = ref(false);
const progress = ref(0);
const bootstrapOpen = ref(false);

const activeCheck = ref<{ check: string; status: VerifyStatus; detail: VerifyDetail } | null>(null);
const drawerOpen = ref(false);

const runs = ref<Run[]>([]);
const runType = ref('');
const runsLoading = ref(false);

/* ── 验证徽章派生 ───────────────────────────────── */
const summaryChecks = computed(() => {
  const map = new Map<string, { check: string; status: VerifyStatus; detail: VerifyDetail }>();
  for (const c of status.value?.verification?.checks ?? []) map.set(c.check, c);
  return map;
});

function checkStatus(name: string): VerifyStatus | 'unknown' {
  return summaryChecks.value.get(name)?.status ?? 'unknown';
}

function toneOf(s: VerifyStatus | 'unknown'): BadgeTone {
  if (s === 'pass') return 'success';
  if (s === 'fail') return 'danger';
  return 'default';
}

/** 未能从 status 取到 check（如最新同步与当前列表不同源）→ 回退到 verifications 最新一条。 */
function detailOf(name: string): { check: string; status: VerifyStatus; detail: VerifyDetail } | null {
  const fromSummary = summaryChecks.value.get(name);
  if (fromSummary) return fromSummary;
  const row = verifications.value.find((v) => v.check === name);
  return row ? { check: row.check, status: row.status, detail: row.detail } : null;
}

function openDetail(name: string): void {
  activeCheck.value = detailOf(name);
  drawerOpen.value = true;
}

/** 豁免（calendar_check 解析）与缺失标的数分行展示。 */
const exemptCount = computed<number | null>(
  () => summaryChecks.value.get('calendar_check')?.detail?.exempt ?? null,
);
const missingCount = computed(
  () => (status.value?.securities ?? []).filter((s) => (s.missing ?? 0) > 0).length,
);

/* ── universe 表 ────────────────────────────────── */
const universeColumns: ApTableColumn[] = [
  { key: 'name', label: '标的' },
  { key: 'last_bar', label: '最后日期' },
  { key: 'lag_days', label: '滞后(天)', align: 'right' },
  { key: 'missing', label: '缺口', align: 'right' },
  { key: 'asset_type', label: '类型' },
];

const TYPE_TONE: Record<string, BadgeTone> = { STOCK: 'info', ETF: 'success', LOF: 'warn' };

const universeRows = computed<Record<string, unknown>[]>(() =>
  (status.value?.securities ?? []).map((s) => ({
    secid: s.secid,
    name: s.name || s.secid,
    last_bar: fmtDate(s.last_bar),
    lag_days: s.lag_days ?? DASH,
    missing: s.missing ?? DASH,
    asset_type: s.asset_type,
  })),
);

/* ── 手动同步 ───────────────────────────────────── */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollUntilDone(jobId: string): Promise<void> {
  for (let i = 0; i < 300; i += 1) {
    const job = await getJob(jobId);
    progress.value = typeof job.progress === 'number' ? job.progress : 0;
    if (job.status === 'done') return;
    if (job.status === 'failed') {
      throw new ApiError('JOB_FAILED', job.error || '同步任务失败', 500);
    }
    await sleep(1000);
  }
  throw new ApiError('JOB_TIMEOUT', '同步任务超时，请在运行历史查看', 504);
}

async function loadStatus(): Promise<void> {
  const [s, v] = await Promise.all([getDataStatus(), listVerifications(20)]);
  status.value = s;
  verifications.value = v;
}

async function loadRuns(): Promise<void> {
  runsLoading.value = true;
  try {
    runs.value = await listRuns({ type: runType.value || undefined, limit: 20 });
  } catch (err) {
    toast.error(`运行历史加载失败：${messageOf(err)}`);
  } finally {
    runsLoading.value = false;
  }
}

function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return '操作失败，请稍后重试';
}

async function runSync(mode: SyncMode): Promise<void> {
  if (syncing.value) return;
  syncing.value = true;
  progress.value = 0;
  try {
    const { job_id } = await syncData({ mode });
    await pollUntilDone(job_id);
    await loadStatus();
    await loadRuns();
    toast.success(mode === 'bootstrap' ? '全量同步完成' : '增量同步完成');
  } catch (err) {
    if (err instanceof ApiError && err.code === 'JOB_LOCKED') {
      toast.info('同 workspace 有同步任务进行中，请稍后重试');
    } else if (err instanceof ApiError && err.code === 'UPSTREAM_RATE_LIMITED') {
      toast.error('上游数据源限流/熔断中，请稍后重试');
    } else {
      toast.error(messageOf(err));
    }
  } finally {
    syncing.value = false;
  }
}

function confirmBootstrap(): void {
  bootstrapOpen.value = false;
  void runSync('bootstrap');
}

/* ── 产物直达 ───────────────────────────────────── */
const downloading = ref('');

function artifactName(key: string): string {
  return key.split('/').pop() ?? key;
}

async function downloadArtifact(run: Run, key: string): Promise<void> {
  const file = artifactName(key);
  downloading.value = `${run.run_uid}:${file}`;
  try {
    const blob = await fetchRunArtifact<Blob>(run.run_uid, file, 'blob');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = file;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    toast.error(`产物下载失败：${messageOf(err)}`);
  } finally {
    downloading.value = '';
  }
}

/* ── 加载 ───────────────────────────────────────── */
async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    await loadStatus();
    await loadRuns();
  } catch (err) {
    error.value = messageOf(err);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row dv-head">
      <div>
        <h1 class="ap-title">数据</h1>
        <p class="ap-page__desc">
          数据截至 <ApBadge tone="info" data-test="asof-badge">{{ fmtDate(status?.as_of) }}</ApBadge>
          · universe <b class="ap-mono" data-test="universe-count">{{ status?.universe?.count ?? DASH }}</b> 标的
        </p>
      </div>
      <div class="ap-row">
        <ApButton
          variant="ghost"
          :disabled="syncing || loading"
          data-test="sync-incremental"
          @click="runSync('incremental')"
        >
          incremental
        </ApButton>
        <ApButton
          variant="danger"
          :disabled="syncing || loading"
          data-test="sync-bootstrap"
          @click="bootstrapOpen = true"
        >
          bootstrap
        </ApButton>
      </div>
    </div>

    <div v-if="syncing" class="dv-progress" data-test="sync-progress">
      <div class="dv-progress__bar">
        <div class="dv-progress__fill" :style="{ width: `${progress}%` }" />
      </div>
      <span class="ap-muted">同步中… {{ progress }}%</span>
    </div>

    <p v-if="loading" class="ap-muted" data-test="dv-loading">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p class="dv-error" role="alert" data-test="dv-error">{{ error }}</p>
      <ApButton variant="ghost" data-test="dv-retry" @click="load">重试</ApButton>
    </ApCard>

    <div v-else class="ap-stack">
      <ApCard title="Universe 数据状态">
        <p class="dv-summary ap-muted">
          <span data-test="exempt-row">日历豁免：<b class="ap-mono">{{ exemptCount ?? DASH }}</b> 天</span>
          <span data-test="missing-row">存在缺口标的：<b class="ap-mono">{{ missingCount }}</b> 个</span>
        </p>
        <ApTable
          :columns="universeColumns"
          :rows="universeRows"
          row-key="secid"
          density="compact"
          empty-text="暂无标的"
          data-test="universe-table"
        >
          <template #cell:name="{ row }">
            <b>{{ row.name }}</b><br /><span class="ap-mono ap-muted">{{ row.secid }}</span>
          </template>
          <template #cell:asset_type="{ row }">
            <ApBadge :tone="TYPE_TONE[String(row.asset_type)] ?? 'default'">
              {{ row.asset_type }}
            </ApBadge>
          </template>
        </ApTable>
      </ApCard>

      <ApCard title="六项验证">
        <div class="ap-row dv-badges">
          <button
            v-for="name in CHECKS"
            :key="name"
            class="dv-badge-btn"
            type="button"
            :data-test="`verify-${name}`"
            @click="openDetail(name)"
          >
            <ApBadge :tone="toneOf(checkStatus(name))">
              {{ name }} · {{ checkStatus(name) }}
            </ApBadge>
          </button>
        </div>
        <p class="ap-muted dv-note">
          汇总状态：
          <b class="ap-mono" data-test="verify-summary">{{ status?.verification?.status ?? 'unknown' }}</b>
          · 来源 job <span class="ap-mono" data-test="verify-job">{{ status?.verification?.job_id ?? DASH }}</span>
        </p>
      </ApCard>

      <ApCard title="运行历史">
        <template #extra>
          <select v-model="runType" class="dv-select" data-test="run-filter" @change="loadRuns">
            <option v-for="t in RUN_TYPES" :key="t" :value="t">{{ t || '全部' }}</option>
          </select>
        </template>
        <p v-if="runsLoading" class="ap-muted" data-test="runs-loading">加载中…</p>
        <ul v-else-if="runs.length" class="dv-runs" data-test="runs-list">
          <li v-for="run in runs" :key="run.run_uid" class="dv-run" data-test="run-item">
            <ApBadge tone="info">{{ run.type }}</ApBadge>
            <span class="ap-mono ap-muted">{{ run.run_uid }}</span>
            <span class="ap-muted">AS_OF {{ fmtDate(run.as_of) }}</span>
            <span class="dv-run__arts">
              <ApButton
                v-for="key in run.artifact_keys"
                :key="key"
                size="sm"
                variant="ghost"
                :loading="downloading === `${run.run_uid}:${artifactName(key)}`"
                :data-test="`artifact-${artifactName(key)}`"
                @click="downloadArtifact(run, key)"
              >
                {{ artifactName(key) }}
              </ApButton>
            </span>
          </li>
        </ul>
        <p v-else class="ap-muted" data-test="runs-empty">暂无运行记录。</p>
      </ApCard>
    </div>

    <ApModal v-model="drawerOpen" :title="activeCheck ? `验证详情 · ${activeCheck.check}` : '验证详情'">
      <div v-if="activeCheck" class="dv-detail" data-test="verify-detail">
        <p class="ap-row dv-detail__head">
          <ApBadge :tone="toneOf(activeCheck.status)">{{ activeCheck.status }}</ApBadge>
          <span class="ap-mono">{{ activeCheck.check }}</span>
        </p>
        <p class="dv-detail__text" data-test="verify-detail-text">{{ activeCheck.detail?.text }}</p>
        <div class="dv-detail__grid">
          <span v-if="activeCheck.detail?.count != null">对账项 <b class="ap-mono">{{ activeCheck.detail.count }}</b></span>
          <span v-if="activeCheck.detail?.exempt != null">豁免 <b class="ap-mono">{{ activeCheck.detail.exempt }}</b></span>
          <span v-if="activeCheck.detail?.sources != null">数据集 <b class="ap-mono">{{ activeCheck.detail.sources }}</b></span>
          <span v-if="activeCheck.detail?.max_diff != null">最大偏差 <b class="ap-mono">{{ activeCheck.detail.max_diff }}</b></span>
          <span v-if="activeCheck.detail?.error">错误 <b class="ap-mono">{{ activeCheck.detail.error }}</b></span>
        </div>
      </div>
      <p v-else class="ap-muted" data-test="verify-detail-empty">暂无该项验证记录。</p>
      <template #footer>
        <ApButton variant="ghost" data-test="verify-close" @click="drawerOpen = false">关闭</ApButton>
      </template>
    </ApModal>

    <ApModal v-model="bootstrapOpen" title="二次确认：全量同步（bootstrap）">
      <div data-test="bootstrap-modal">
        <p>全量同步将重新拉取 universe 全部标的的历史行情，<b>耗时较长且消耗上游额度</b>。</p>
        <p class="ap-muted">建议仅在初次建库或数据大面积缺失时使用；日常请用 incremental。</p>
      </div>
      <template #footer>
        <ApButton variant="ghost" data-test="bootstrap-cancel" @click="bootstrapOpen = false">取消</ApButton>
        <ApButton variant="danger" data-test="bootstrap-yes" @click="confirmBootstrap">开始全量同步</ApButton>
      </template>
    </ApModal>
  </main>
</template>

<style scoped>
.dv-head {
  justify-content: space-between;
  align-items: flex-start;
}
.dv-progress {
  display: flex;
  align-items: center;
  gap: var(--ap-sp-3);
  margin-bottom: var(--ap-sp-4);
  font-size: var(--ap-fs-sm);
}
.dv-progress__bar {
  flex: 1;
  height: 6px;
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: 999px;
  overflow: hidden;
}
.dv-progress__fill {
  height: 100%;
  background: var(--ap-seal);
  transition: width 0.3s ease;
}
.dv-error {
  color: var(--ap-danger);
  margin-bottom: var(--ap-sp-3);
}
.dv-summary {
  display: flex;
  gap: var(--ap-sp-4);
  flex-wrap: wrap;
  margin-bottom: var(--ap-sp-3);
  font-size: var(--ap-fs-sm);
}
.dv-badges {
  gap: var(--ap-sp-2);
  flex-wrap: wrap;
}
.dv-badge-btn {
  appearance: none;
  border: 0;
  background: transparent;
  padding: 0;
  cursor: pointer;
}
.dv-note {
  margin-top: var(--ap-sp-3);
  font-size: var(--ap-fs-sm);
}
.dv-select {
  height: 28px;
  padding: 0 var(--ap-sp-2);
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  color: var(--ap-ink);
  font-size: var(--ap-fs-sm);
}
.dv-runs {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
}
.dv-run {
  display: flex;
  align-items: center;
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  padding: 7px 0;
  border-bottom: 1px solid var(--ap-hair);
  font-size: var(--ap-fs-sm);
}
.dv-run:last-child {
  border-bottom: 0;
}
.dv-run__arts {
  display: flex;
  gap: var(--ap-sp-2);
  flex-wrap: wrap;
  margin-left: auto;
}
.dv-detail__head {
  gap: var(--ap-sp-2);
  align-items: center;
  margin-bottom: var(--ap-sp-2);
}
.dv-detail__text {
  padding: 8px 10px;
  background: var(--ap-card2);
  border-radius: var(--ap-radius-sm);
  font-family: var(--ap-font-mono);
  font-size: var(--ap-fs-sm);
  word-break: break-all;
}
.dv-detail__grid {
  display: flex;
  gap: var(--ap-sp-4);
  flex-wrap: wrap;
  margin-top: var(--ap-sp-3);
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
</style>
