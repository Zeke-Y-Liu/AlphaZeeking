<script setup lang="ts">
// 调仓页（T6.6）：最新 plan（diff_trades 理由链 + E2 徽章）→ 生成执行单（资金输入）
// → 委托表（CSV 下载）→ 纪律确认流（勾选 + 二次确认 Modal）→ 成交登记 → 状态时间线。
// QDII 溢价警示条：组合含 QDII 时常显（来源为最近一次 review.json 的 industry）。
import { computed, onMounted, reactive, ref } from 'vue';
import { useRoute } from 'vue-router';

import {
  cancelExecOrder,
  confirmExecOrder,
  createExecOrders,
  createRebalancePlan,
  fetchRunArtifact,
  fillExecOrder,
  getExecOrder,
  getJob,
  getPlan,
  getPortfolio,
  listExecOrders,
  listPlans,
  listRuns,
} from '../api';
import { ApiError } from '../api/types';
import type {
  ExecOrder,
  ExecOrderFill,
  ExecOrderRow,
  ExecOrderStatus,
  Portfolio,
  RebalancePlan,
  RebalancePlanSummary,
  ReviewPayload,
} from '../api/types';
import { ApBadge, ApButton, ApCard, ApModal, ApTable, useToast } from '../components/ui';
import type { ApTableColumn, BadgeTone } from '../components/ui';
import { DASH, fmtCny, fmtDate, fmtNum } from '../utils/format';

const route = useRoute();
const toast = useToast();

const pfId = computed(() => String(route.params.id));
const DEFAULT_CAPITAL = 1_000_000;
const LOT = 100;

/* ── 状态 ───────────────────────────────────────── */
const pf = ref<Portfolio | null>(null);
const plan = ref<RebalancePlan | null>(null);
const execOrder = ref<ExecOrder | null>(null);
const loading = ref(true);
const error = ref('');
const hasQdii = ref(false);

const rerunning = ref(false);
const planProgress = ref(0);
const generating = ref(false);
const confirming = ref(false);
const filling = ref(false);
const cancelling = ref(false);
const downloading = ref(false);

const capital = ref<number>(DEFAULT_CAPITAL);
const ack = ref(false);
const confirmOpen = ref(false);
const fillError = ref('');

/** 成交登记表单：secid → {price, shares}（默认取委托值）。 */
const fillRows = reactive<Record<string, { price: number; shares: number }>>({});

/* ── 常量映射 ───────────────────────────────────── */
const STATUS_TONE: Record<ExecOrderStatus, BadgeTone> = {
  draft: 'warn',
  confirmed: 'info',
  executed: 'success',
  cancelled: 'default',
};
const STATUS_TEXT: Record<ExecOrderStatus, string> = {
  draft: '待确认',
  confirmed: '已确认',
  executed: '已成交',
  cancelled: '已取消',
};
const ACTION_TEXT: Record<string, string> = {
  create: '生成执行单',
  confirm: '确认执行单',
  fill: '登记成交',
  cancel: '取消执行单',
};

const sheet = computed(() => execOrder.value?.sheet ?? null);
const actionable = computed<ExecOrderRow[]>(() => sheet.value?.actionable ?? []);

/** 委托表列（# 方向/标的/数量与手数/金额/费用/结算/限价参考带 + 权重/现价）。 */
const orderColumns: ApTableColumn[] = [
  { key: 'instruction', label: '指令' },
  { key: 'name', label: '标的' },
  { key: 'direction', label: '方向' },
  { key: 'weights', label: '现→目标', align: 'right' },
  { key: 'price', label: '现价', align: 'right' },
  { key: 'order_shares', label: '委托数量', align: 'right' },
  { key: 'lots', label: '手数', align: 'right' },
  { key: 'order_amount', label: '委托金额', align: 'right' },
  { key: 'fee', label: '费用', align: 'right' },
  { key: 'settle', label: '结算' },
  { key: 'band', label: '限价参考带', align: 'right' },
];

const orderRows = computed<Record<string, unknown>[]>(() =>
  (sheet.value?.orders ?? []).map((o) => ({
    secid: o.secid,
    instruction: o.instruction,
    name: o.name,
    direction: o.direction,
    weights: `${fmtNum(o.cur_weight, 1)}% → ${fmtNum(o.tgt_weight, 1)}%`,
    price: fmtNum(o.price, 3),
    order_shares: o.order_shares === 0 ? DASH : `${o.order_shares > 0 ? '+' : ''}${o.order_shares.toLocaleString()}`,
    lots: o.order_shares === 0 ? DASH : String(o.lots),
    order_amount: o.order_shares === 0 ? DASH : fmtCny(o.order_amount),
    fee: o.order_shares === 0 ? DASH : fmtNum(o.fee, 2),
    settle: o.settle,
    band: `${fmtNum(o.band_low, 3)}~${fmtNum(o.band_high, 3)}`,
    reason: o.reason,
    evidence: o.evidence,
    state: o.state,
  })),
);

/* ── 加载 ───────────────────────────────────────── */
async function loadPlan(): Promise<void> {
  const plans: RebalancePlanSummary[] = await listPlans(pfId.value);
  const head = plans[0] ?? null;
  plan.value = head ? await getPlan(head.id) : null;
}

async function loadExecOrder(): Promise<void> {
  const orders = await listExecOrders(pfId.value);
  const match = plan.value
    ? orders.find((o) => o.plan_id === plan.value?.id) ?? orders[0]
    : orders[0];
  execOrder.value = match ? await getExecOrder(match.exec_uid) : null;
  resetFillRows();
}

function resetFillRows(): void {
  for (const key of Object.keys(fillRows)) delete fillRows[key];
  for (const o of actionable.value) {
    fillRows[o.secid] = { price: o.price, shares: o.order_shares };
  }
}

/** QDII 检测：读最近一次 review.json 的 industry（best-effort，失败不阻断）。 */
async function detectQdii(): Promise<void> {
  const pid = pf.value?.id;
  if (pid == null) return;
  try {
    // runs 过滤按数值主键（pid 为 'cn4' 等短码，Number(pid) 会得到 NaN → 422）
    const runs = await listRuns({ type: 'review', portfolio_id: pid, limit: 1 });
    if (!runs[0]) return;
    const review = await fetchRunArtifact<ReviewPayload>(runs[0].run_uid, 'review.json', 'json');
    hasQdii.value = (review.positions ?? []).some((p) => (p.industry ?? '').includes('QDII'));
  } catch {
    hasQdii.value = false;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    pf.value = await getPortfolio(pfId.value);
    await loadPlan();
    await loadExecOrder();
    void detectQdii();
  } catch (err) {
    error.value = messageOf(err);
  } finally {
    loading.value = false;
  }
}

/* ── 错误/轮询工具 ──────────────────────────────── */
function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return '操作失败，请稍后重试';
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollUntilDone(jobId: string): Promise<void> {
  for (let i = 0; i < 120; i += 1) {
    const job = await getJob(jobId);
    planProgress.value = typeof job.progress === 'number' ? job.progress : 0;
    if (job.status === 'done') return;
    if (job.status === 'failed') {
      throw new ApiError('JOB_FAILED', job.error || '调仓评估任务失败', 500);
    }
    await sleep(1000);
  }
  throw new ApiError('JOB_TIMEOUT', '调仓评估任务超时，请稍后在运行历史查看', 504);
}

/* ── 生成/重跑 plan ─────────────────────────────── */
async function rerunPlan(): Promise<void> {
  if (rerunning.value) return;
  rerunning.value = true;
  planProgress.value = 0;
  try {
    const { job_id } = await createRebalancePlan(pfId.value);
    await pollUntilDone(job_id);
    await load();
    toast.success('调仓评估单已生成');
  } catch (err) {
    if (err instanceof ApiError && err.code === 'VERIFY_GATE_FAILED') {
      toast.error('数据验证未全绿，调仓评估被门禁拦截，请前往数据页');
    } else if (err instanceof ApiError && err.code === 'JOB_LOCKED') {
      toast.info('同 workspace 有计算任务进行中，请稍后重试');
    } else {
      toast.error(messageOf(err));
    }
  } finally {
    rerunning.value = false;
  }
}

/* ── 生成执行单 ─────────────────────────────────── */
async function generateExec(): Promise<void> {
  if (!plan.value || generating.value) return;
  if (!(capital.value > 0)) {
    toast.error('基准资金须大于 0');
    return;
  }
  generating.value = true;
  fillError.value = '';
  ack.value = false;
  try {
    execOrder.value = await createExecOrders(plan.value.id, { capital_cny: Number(capital.value) });
    resetFillRows();
    toast.success('执行单已生成，请逐条核对后确认');
  } catch (err) {
    if (err instanceof ApiError && err.code === 'DATA_STALE') {
      toast.error('调仓计划已过期（存在更新的交易日），请先重跑调仓评估');
    } else {
      toast.error(messageOf(err));
    }
  } finally {
    generating.value = false;
  }
}

/* ── CSV 下载 ───────────────────────────────────── */
async function downloadCsv(): Promise<void> {
  if (!execOrder.value || downloading.value) return;
  downloading.value = true;
  try {
    const blob = await fetchRunArtifact<Blob>(execOrder.value.exec_uid, 'exec_order.csv', 'blob');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `exec_order_${execOrder.value.sheet.exec_id}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    toast.error(`CSV 下载失败：${messageOf(err)}`);
  } finally {
    downloading.value = false;
  }
}

/* ── 确认流 ─────────────────────────────────────── */
function openConfirm(): void {
  if (!ack.value) {
    toast.error('请先勾选纪律声明');
    return;
  }
  confirmOpen.value = true;
}

async function doConfirm(): Promise<void> {
  if (!execOrder.value) return;
  confirming.value = true;
  try {
    execOrder.value = await confirmExecOrder(execOrder.value.exec_uid, { ack_discipline: true });
    confirmOpen.value = false;
    toast.success('执行单已确认');
  } catch (err) {
    toast.error(messageOf(err));
  } finally {
    confirming.value = false;
  }
}

/* ── 成交登记 ───────────────────────────────────── */
function fillInvalid(o: ExecOrderRow): boolean {
  const row = fillRows[o.secid];
  if (!row) return true;
  if (!(row.price > 0)) return true;
  if (!row.shares || row.shares % LOT !== 0) return true;
  return (row.shares > 0) !== (o.order_shares > 0);
}

const allFillsValid = computed(
  () => actionable.value.length > 0 && actionable.value.every((o) => !fillInvalid(o)),
);

async function submitFills(): Promise<void> {
  if (!execOrder.value || filling.value) return;
  const fills: ExecOrderFill[] = actionable.value.map((o) => ({
    secid: o.secid,
    price: Number(fillRows[o.secid]?.price ?? 0),
    shares: Number(fillRows[o.secid]?.shares ?? 0),
  }));
  if (fills.some((f) => !(f.price > 0) || f.shares % LOT !== 0 || f.shares === 0)) {
    fillError.value = `价格须大于 0，数量须为 ${LOT} 的整数倍且方向与委托一致`;
    return;
  }
  filling.value = true;
  fillError.value = '';
  try {
    execOrder.value = await fillExecOrder(execOrder.value.exec_uid, { fills });
    toast.success(execOrder.value.fills?.partial ? '成交已登记（部分成交）' : '成交已登记');
  } catch (err) {
    fillError.value = messageOf(err);
    toast.error(messageOf(err));
  } finally {
    filling.value = false;
  }
}

async function cancelOrder(): Promise<void> {
  if (!execOrder.value || cancelling.value) return;
  cancelling.value = true;
  try {
    execOrder.value = await cancelExecOrder(execOrder.value.exec_uid, { reason: '用户取消' });
    toast.info('执行单已取消');
  } catch (err) {
    toast.error(messageOf(err));
  } finally {
    cancelling.value = false;
  }
}

/* ── 展示派生 ───────────────────────────────────── */
const canGenerate = computed(() => plan.value?.status === 'draft');
const canConfirm = computed(() => execOrder.value?.status === 'draft');
const canFill = computed(() => execOrder.value?.status === 'confirmed');

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row rb-head">
      <div>
        <h1 class="ap-title">调仓 · {{ pf?.name ?? pfId }}</h1>
        <p class="ap-page__desc">引擎 B 触发 → 拟调仓单 → 执行单 → 人工确认 → 成交登记（引擎永不直接动仓位）。</p>
      </div>
      <ApButton
        variant="ghost"
        :loading="rerunning"
        :disabled="loading || rerunning"
        data-test="rerun-plan"
        @click="rerunPlan"
      >
        重跑调仓评估
      </ApButton>
    </div>

    <div v-if="hasQdii" class="rb-qdii" role="alert" data-test="qdii-warn">
      <ApBadge tone="warn">QDII 溢价核对</ApBadge>
      <span>组合含跨境品种：ETF 场内价可偏离净值，下单前核对 IOPV 溢价，溢价 &gt; 1% 建议暂缓。</span>
    </div>

    <p v-if="rerunning" class="ap-muted rb-note" data-test="plan-progress">
      调仓评估中… {{ planProgress }}%
    </p>

    <p v-if="loading" class="ap-muted" data-test="rb-loading">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p class="rb-error" role="alert" data-test="rb-error">{{ error }}</p>
      <ApButton variant="ghost" data-test="rb-retry" @click="load">重试</ApButton>
    </ApCard>

    <div v-else class="ap-stack">
      <!-- 最新 plan 卡 -->
      <ApCard title="最新调仓评估单" data-test="plan-card">
        <template #extra>
          <ApBadge v-if="plan" :tone="plan.status === 'draft' ? 'warn' : 'default'" data-test="plan-status">
            {{ plan.status }}
          </ApBadge>
        </template>

        <p v-if="!plan" class="ap-muted" data-test="plan-empty">
          暂无调仓评估单，请点击「重跑调仓评估」生成。
        </p>

        <template v-else>
          <div class="rb-plan__meta ap-row">
            <span class="ap-mono" data-test="plan-uid">{{ plan.plan_uid }}</span>
            <span class="ap-muted">AS_OF <b class="ap-mono">{{ fmtDate(plan.payload?.as_of) }}</b></span>
            <span class="ap-muted">预计换手 <b class="ap-mono">{{ plan.payload?.turnover_est ?? DASH }}</b></span>
            <span class="ap-muted">成本预估 <b class="ap-mono">{{ fmtCny(plan.payload?.cost_est_cny) }}</b></span>
          </div>

          <p v-if="!plan.payload?.diff_trades?.length" class="ap-muted" data-test="plan-no-trades">
            无触发调仓（引擎 B 未触发卖出预警）。
          </p>
          <ul v-else class="rb-trades" data-test="plan-diff">
            <li v-for="t in plan.payload.diff_trades" :key="t.secid" class="rb-trade" data-test="plan-trade">
              <ApBadge :tone="t.side === 'evaluate_sell' ? 'danger' : 'success'">{{ t.side }}</ApBadge>
              <span class="rb-trade__name" :data-test="`plan-reason-${t.secid}`">
                <b>{{ t.name }}</b> <span class="ap-mono ap-muted">{{ t.secid }}</span>
                <span class="ap-mono">{{ fmtNum(t.from, 1) }}% → {{ fmtNum(t.to, 1) }}%</span>
                <span class="rb-trade__reason">{{ t.reason }}</span>
              </span>
              <ApBadge tone="info" data-test="plan-evidence">证据 {{ t.evidence }}</ApBadge>
            </li>
          </ul>
        </template>
      </ApCard>

      <!-- 执行单生成 -->
      <ApCard title="执行单" data-test="exec-card">
        <div class="rb-gen ap-row">
          <label class="rb-field">
            <span class="rb-label">基准资金 (¥)</span>
            <input
              v-model.number="capital"
              class="rb-input ap-mono"
              type="number"
              min="1"
              step="10000"
              data-test="capital-input"
            />
          </label>
          <ApButton
            :loading="generating"
            :disabled="!plan || !canGenerate || generating"
            data-test="gen-exec"
            @click="generateExec"
          >
            生成执行单
          </ApButton>
        </div>

        <p v-if="!execOrder" class="ap-muted rb-note" data-test="exec-empty">
          尚无执行单：确认 plan 为 draft 后点击「生成执行单」。
        </p>

        <template v-else>
          <div class="rb-summary ap-row" data-test="exec-summary">
            <ApBadge :tone="STATUS_TONE[execOrder.status]" data-test="exec-status">
              {{ STATUS_TEXT[execOrder.status] }}
            </ApBadge>
            <span class="ap-mono" data-test="exec-id">{{ sheet?.exec_id }}</span>
            <span class="ap-muted">下一交易日 <b class="ap-mono">{{ fmtDate(sheet?.next_trade_date) }}</b></span>
            <span class="ap-muted">委托 <b class="ap-mono" data-test="n-orders">{{ sheet?.summary?.n_orders }}</b> 笔</span>
            <span class="ap-muted">
              换手 <b class="ap-mono">{{ sheet?.summary?.turnover_pct }}%</b>
            </span>
            <span class="ap-muted">
              费用 <b class="ap-mono" data-test="total-fee">{{ fmtNum(sheet?.fees?.total_fee_cny, 2) }}</b>
            </span>
          </div>

          <div class="rb-cash ap-row">
            <span>卖出回款 <b class="ap-mono">{{ fmtCny(sheet?.cash?.sell_proceeds) }}</b></span>
            <span>买入支出 <b class="ap-mono">{{ fmtCny(sheet?.cash?.buy_outlay) }}</b></span>
            <span>净现金变动 <b class="ap-mono">{{ fmtCny(sheet?.cash?.net_cash) }}</b></span>
            <span>期末现金占比 <b class="ap-mono">{{ fmtNum(sheet?.cash?.end_cash_pct, 1) }}%</b></span>
          </div>

          <div class="rb-toolbar ap-row">
            <ApButton variant="ghost" size="sm" :loading="downloading" data-test="csv-download" @click="downloadCsv">
              下载 CSV
            </ApButton>
          </div>

          <ApTable
            :columns="orderColumns"
            :rows="orderRows"
            row-key="secid"
            density="compact"
            data-test="exec-table"
          >
            <template #cell:instruction="{ row }">
              <ApBadge :tone="row.instruction === 'evaluate_sell' ? 'danger' : 'default'">
                {{ row.instruction }}
              </ApBadge>
            </template>
            <template #cell:name="{ row }">
              <b>{{ row.name }}</b><br /><span class="ap-mono ap-muted">{{ row.secid }}</span>
            </template>
            <template #cell:order_shares="{ row }">
              <span class="ap-mono" :data-test="`order-shares-${String(row.secid)}`">{{ row.order_shares }}</span>
            </template>
            <template #cell:order_amount="{ row }">
              <span class="ap-mono" :data-test="`order-amount-${String(row.secid)}`">{{ row.order_amount }}</span>
            </template>
          </ApTable>

          <!-- 确认流 -->
          <div v-if="canConfirm" class="rb-confirm" data-test="confirm-flow">
            <label class="rb-check">
              <input v-model="ack" type="checkbox" data-test="ack-check" />
              <span>我已阅读并确认纪律声明：引擎永不直接动仓位，本执行单须人工逐条核对后在券商端下单。</span>
            </label>
            <div class="rb-actions ap-row">
              <ApButton variant="danger" :loading="cancelling" data-test="cancel-btn" @click="cancelOrder">取消执行单</ApButton>
              <ApButton :disabled="!ack" data-test="confirm-btn" @click="openConfirm">确认执行单</ApButton>
            </div>
          </div>

          <!-- 成交登记 -->
          <div v-else-if="canFill" class="rb-fill" data-test="fill-flow">
            <p class="rb-label">成交登记（数量须为 {{ LOT }} 的整数倍；允许部分成交）</p>
            <ul class="rb-fill__list">
              <li v-for="o in actionable" :key="o.secid" class="rb-fill__row" data-test="fill-row">
                <span class="ap-mono">{{ o.name }}</span>
                <span class="ap-muted ap-mono">{{ o.secid }}</span>
                <span class="ap-muted">委托 {{ o.order_shares.toLocaleString() }}</span>
                <label class="rb-fill__field">
                  <span class="rb-label">价格</span>
                  <input
                    v-model.number="fillRows[o.secid].price"
                    class="rb-input ap-mono"
                    type="number"
                    step="0.001"
                    min="0"
                    :data-test="`fill-price-${o.secid}`"
                  />
                </label>
                <label class="rb-fill__field">
                  <span class="rb-label">数量</span>
                  <input
                    v-model.number="fillRows[o.secid].shares"
                    class="rb-input ap-mono"
                    type="number"
                    step="100"
                    :data-test="`fill-shares-${o.secid}`"
                  />
                </label>
                <span v-if="fillInvalid(o)" class="rb-fill__bad" data-test="fill-invalid">校验未过</span>
              </li>
            </ul>
            <p v-if="fillError" class="rb-error" role="alert" data-test="fill-error">{{ fillError }}</p>
            <div class="rb-actions ap-row">
              <ApButton variant="danger" :loading="cancelling" data-test="cancel-btn" @click="cancelOrder">取消执行单</ApButton>
              <ApButton :loading="filling" :disabled="!allFillsValid || filling" data-test="submit-fills" @click="submitFills">
                登记成交
              </ApButton>
            </div>
          </div>

          <!-- 已成交摘要 -->
          <div v-else-if="execOrder.fills" class="rb-fills" data-test="fills-summary">
            <p class="rb-label">
              已登记成交 {{ execOrder.fills.n }} 笔 · 合计 <b class="ap-mono">{{ fmtCny(execOrder.fills.total_amount) }}</b>
              <ApBadge v-if="execOrder.fills.partial" tone="warn">部分成交</ApBadge>
            </p>
          </div>

          <!-- 状态时间线 -->
          <div class="rb-timeline" data-test="timeline">
            <p class="rb-label">状态时间线</p>
            <ol class="rb-timeline__list">
              <li v-for="(item, i) in execOrder.timeline" :key="i" class="rb-timeline__item" data-test="timeline-item">
                <span class="ap-mono rb-timeline__at">{{ fmtDate(item.at) }}</span>
                <span class="rb-timeline__act" :data-test="`timeline-action-${item.action}`">
                  {{ ACTION_TEXT[item.action] ?? item.action }}
                </span>
                <span class="ap-muted">{{ item.actor }}</span>
              </li>
            </ol>
          </div>
        </template>
      </ApCard>
    </div>

    <ApModal v-model="confirmOpen" title="二次确认：确认执行单">
      <div data-test="confirm-modal">
        <p>确认后执行单进入 <b>confirmed</b>，即可登记成交；确认动作将写入审计时间线。</p>
        <p class="ap-muted">引擎永不直接动仓位：请在券商端依据本执行单人工下单。</p>
      </div>
      <template #footer>
        <ApButton variant="ghost" data-test="confirm-cancel" @click="confirmOpen = false">返回</ApButton>
        <ApButton :loading="confirming" data-test="confirm-yes" @click="doConfirm">确认</ApButton>
      </template>
    </ApModal>
  </main>
</template>

<style scoped>
.rb-head {
  justify-content: space-between;
  align-items: flex-start;
}
.rb-qdii {
  display: flex;
  align-items: center;
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  margin-bottom: var(--ap-sp-4);
  padding: 9px 12px;
  border: 1px solid rgba(176, 140, 60, 0.45);
  background: rgba(176, 140, 60, 0.1);
  border-radius: var(--ap-radius);
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
.rb-note {
  margin: var(--ap-sp-3) 0;
}
.rb-error {
  color: var(--ap-danger);
  margin-top: var(--ap-sp-2);
}
.rb-plan__meta {
  gap: var(--ap-sp-4);
  flex-wrap: wrap;
  margin-bottom: var(--ap-sp-3);
  font-size: var(--ap-fs-sm);
}
.rb-trades {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
}
.rb-trade {
  display: flex;
  align-items: flex-start;
  gap: var(--ap-sp-2);
  padding: 7px 0;
  border-bottom: 1px solid var(--ap-hair);
  font-size: var(--ap-fs-sm);
}
.rb-trade:last-child {
  border-bottom: 0;
}
.rb-trade__name {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}
.rb-trade__reason {
  color: var(--ap-dim);
  font-size: var(--ap-fs-xs);
}
.rb-gen {
  gap: var(--ap-sp-3);
  align-items: flex-end;
  margin-bottom: var(--ap-sp-3);
}
.rb-field,
.rb-fill__field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.rb-label {
  font-family: var(--ap-font-mono);
  font-size: 9px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ap-mute);
}
.rb-input {
  height: 32px;
  width: 150px;
  padding: 0 var(--ap-sp-3);
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  color: var(--ap-ink);
  font-size: var(--ap-fs-sm);
}
.rb-input:focus {
  outline: none;
  border-color: var(--ap-seal);
  box-shadow: 0 0 0 3px var(--ap-seal-soft);
}
.rb-summary {
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: var(--ap-sp-2);
  font-size: var(--ap-fs-sm);
}
.rb-cash {
  gap: var(--ap-sp-4);
  flex-wrap: wrap;
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
.rb-toolbar {
  justify-content: flex-end;
  margin: var(--ap-sp-3) 0;
}
.rb-confirm {
  margin-top: var(--ap-sp-4);
  padding-top: var(--ap-sp-3);
  border-top: 1px solid var(--ap-hair);
}
.rb-check {
  display: flex;
  align-items: flex-start;
  gap: var(--ap-sp-2);
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
.rb-actions {
  justify-content: flex-end;
  gap: var(--ap-sp-2);
  margin-top: var(--ap-sp-3);
}
.rb-fill__list {
  margin: var(--ap-sp-2) 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
}
.rb-fill__row {
  display: flex;
  align-items: flex-end;
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  font-size: var(--ap-fs-sm);
}
.rb-fill__bad {
  color: var(--ap-danger);
  font-size: var(--ap-fs-xs);
}
.rb-fills {
  margin-top: var(--ap-sp-3);
}
.rb-timeline {
  margin-top: var(--ap-sp-4);
  padding-top: var(--ap-sp-3);
  border-top: 1px solid var(--ap-hair);
}
.rb-timeline__list {
  margin: var(--ap-sp-2) 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
}
.rb-timeline__item {
  display: flex;
  align-items: center;
  gap: var(--ap-sp-3);
  font-size: var(--ap-fs-sm);
}
.rb-timeline__at {
  color: var(--ap-mute);
  font-size: var(--ap-fs-xs);
}
.rb-timeline__act {
  font-weight: 600;
}
</style>
