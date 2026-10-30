<script setup lang="ts">
// 引擎板块：三引擎自描述卡 + 指标注册表增删改查（配置即数据，注册表 v2）。
// - Tab 切换引擎（A/B/C）+ 状态过滤；指标表含 id/公式/维度/数据集/状态/版本；
// - 新增/编辑弹窗（hypothesis_id 必填 = 研究门禁；formula 须 builtin 清单）；
// - 数据缺失联动：新增/修改响应携带 data_missing → 提示条 + 前往数据页同步；
// - 停用/启用/软删均走注册表 changelog（version 自增，条目保留可复活）。
import { computed, onMounted, ref } from 'vue';

import {
  addEngineMetric,
  describeEngines,
  listEngineDatasets,
  listEngineMetrics,
  removeEngineMetric,
  updateEngineMetric,
} from '../api';
import type {
  EngineDataset,
  EngineDescribe,
  EngineMetric,
  MetricDataMissing,
} from '../api/types';
import { ApBadge, ApButton, ApCard, ApModal, ApTable, useToast } from '../components/ui';
import type { ApTableColumn, BadgeTone } from '../components/ui';
import { DASH, fmtDate } from '../utils/format';

const toast = useToast();

/* ── 引擎描述（顶部三卡） ─────────────────────── */
const ENGINE_LABEL: Record<string, string> = {
  alpha: '引擎A · 中长期因子',
  beta: '引擎B · 20日信号',
  macro: '引擎C · 宏观流动性',
};
const describes = ref<EngineDescribe[]>([]);
const activeEngine = ref('alpha');
const statusFilter = ref('');

/* ── 指标清单 ─────────────────────────────────── */
const metrics = ref<EngineMetric[]>([]);
const changelogN = ref(0);
const loading = ref(true);
const error = ref('');

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const [desc, list] = await Promise.all([
      describes.value.length ? Promise.resolve({ engines: describes.value }) : describeEngines(),
      listEngineMetrics({ engine: activeEngine.value, status: statusFilter.value || undefined }),
    ]);
    describes.value = desc.engines;
    metrics.value = list.metrics;
    changelogN.value = list.changelog_n;
  } catch (err) {
    error.value = messageOf(err);
  } finally {
    loading.value = false;
  }
}

function switchEngine(id: string): void {
  activeEngine.value = id;
  void load();
}

/* ── 表格 ─────────────────────────────────────── */
const columns: ApTableColumn[] = [
  { key: 'id', label: '指标' },
  { key: 'formula', label: '公式' },
  { key: 'dim', label: '维度' },
  { key: 'market', label: '市场' },
  { key: 'dataset', label: '数据集' },
  { key: 'status', label: '状态' },
  { key: 'version', label: '版本', align: 'right' },
  { key: 'actions', label: '操作' },
];

const rows = computed<Record<string, unknown>[]>(() =>
  metrics.value.map((m) => ({
    ...m,
    dim: m.dim || DASH,
    market: m.market || DASH,
    dataset: m.dataset || DASH,
  })),
);

function statusTone(s: string): BadgeTone {
  return s === 'active' ? 'success' : s === 'inactive' ? 'default' : 'warn';
}

/* ── 数据缺失提示（新增/修改响应联动） ─────────── */
const dataMissing = ref<MetricDataMissing[]>([]);

function showMissing(items: MetricDataMissing[] | undefined, verb: string): void {
  dataMissing.value = items ?? [];
  if (items?.length) {
    toast.info(`指标已${verb}，但其数据集尚未同步（见页面顶部提示）`);
  }
}

/* ── 新增弹窗 ─────────────────────────────────── */
const addOpen = ref(false);
const addForm = ref({
  id: '',
  formula: '',
  dim: '',
  market: '',
  dataset: '',
  scorerType: '',
  scorerArg: '',
  note: '',
  hypothesis_id: '',
  reason: '',
});
const submitting = ref(false);

function openAdd(): void {
  addForm.value = {
    id: '',
    formula: activeEngine.value === 'macro' ? 'builtin:macro_series' : '',
    dim: '',
    market: activeEngine.value === 'macro' ? 'A' : '',
    dataset: activeEngine.value === 'macro' ? 'macro_liquidity' : '',
    scorerType: '',
    scorerArg: '',
    note: '',
    hypothesis_id: '',
    reason: '',
  };
  addOpen.value = true;
}

async function submitAdd(): Promise<void> {
  const f = addForm.value;
  if (!f.id || !f.formula || !f.hypothesis_id) {
    toast.error('指标 id、公式、hypothesis_id 为必填项');
    return;
  }
  submitting.value = true;
  try {
    // scorer 形如 {"type": "step", "arg": [8, 10]}：arg 按 JSON 数组解析，失败按单值
    let scorer: Record<string, unknown> | null = null;
    if (f.scorerType) {
      let arg: unknown = f.scorerArg;
      try {
        arg = JSON.parse(f.scorerArg);
      } catch {
        /* 单值保留原样 */
      }
      scorer = { type: f.scorerType, arg };
    }
    const res = await addEngineMetric({
      engine: activeEngine.value,
      spec: {
        id: f.id.trim(),
        formula: f.formula.trim(),
        dim: f.dim || null,
        market: f.market || null,
        dataset: f.dataset || null,
        scorer,
        params: {},
        status: 'active',
        note: f.note,
      },
      hypothesis_id: f.hypothesis_id.trim(),
      reason: f.reason,
    });
    addOpen.value = false;
    toast.success(`指标 ${res.metric.id} 已新增（v${res.metric.version}）`);
    showMissing(res.data_missing, '新增');
    await load();
  } catch (err) {
    toast.error(`新增失败：${messageOf(err)}`);
  } finally {
    submitting.value = false;
  }
}

/* ── 编辑弹窗（note/维度/数据集/参数说明，轻量面） ── */
const editOpen = ref(false);
const editTarget = ref<EngineMetric | null>(null);
const editForm = ref({ dim: '', dataset: '', note: '', reason: '' });

function openEdit(m: EngineMetric): void {
  editTarget.value = m;
  editForm.value = { dim: m.dim || '', dataset: m.dataset || '', note: m.note || '', reason: '' };
  editOpen.value = true;
}

async function submitEdit(): Promise<void> {
  const t = editTarget.value;
  if (!t) return;
  const patch: Record<string, unknown> = { note: editForm.value.note };
  if (activeEngine.value === 'macro') {
    patch.dim = editForm.value.dim || null;
    patch.dataset = editForm.value.dataset || null;
  }
  submitting.value = true;
  try {
    const res = await updateEngineMetric(t.id, { patch, reason: editForm.value.reason });
    editOpen.value = false;
    toast.success(`指标 ${t.id} 已更新（v${res.after.version}）`);
    showMissing(res.data_missing, '更新');
    await load();
  } catch (err) {
    toast.error(`更新失败：${messageOf(err)}`);
  } finally {
    submitting.value = false;
  }
}

/* ── 停用/启用/软删 ───────────────────────────── */
async function toggleStatus(m: EngineMetric): Promise<void> {
  const to = m.status === 'active' ? 'inactive' : 'active';
  try {
    const res = await updateEngineMetric(m.id, {
      patch: { status: to },
      // 启停翻转走研究门禁：复用指标登记的 hypothesis_id，未登记需补
      hypothesis_id: m.hypothesis_id || undefined,
      reason: to === 'active' ? '启用' : '停用',
    });
    toast.success(`指标 ${m.id} 已${to === 'active' ? '启用' : '停用'}（v${res.after.version}）`);
    await load();
  } catch (err) {
    toast.error(`${to === 'active' ? '启用' : '停用'}失败：${messageOf(err)}`);
  }
}

const removeOpen = ref(false);
const removeTarget = ref<EngineMetric | null>(null);
const removeForm = ref({ hypothesis_id: '', reason: '' });

async function submitRemove(): Promise<void> {
  const t = removeTarget.value;
  if (!t) return;
  if (!removeForm.value.hypothesis_id) {
    toast.error('软删需 hypothesis_id（研究门禁）');
    return;
  }
  submitting.value = true;
  try {
    await removeEngineMetric(t.id, {
      hypothesis_id: removeForm.value.hypothesis_id.trim(),
      reason: removeForm.value.reason,
    });
    removeOpen.value = false;
    removeTarget.value = null;
    toast.success(`指标 ${t.id} 已软删（status=inactive，可复活）`);
    await load();
  } catch (err) {
    toast.error(`删除失败：${messageOf(err)}`);
  } finally {
    submitting.value = false;
  }
}

/* ── 数据集状态（右卡：与数据页共用端点） ───────── */
const engineDatasets = ref<EngineDataset[]>([]);
const datasetsMissing = ref(0);

async function loadDatasets(): Promise<void> {
  try {
    const res = await listEngineDatasets();
    engineDatasets.value = res.datasets;
    datasetsMissing.value = res.missing;
  } catch {
    /* 数据集状态为增强信息：失败不打断页面 */
  }
}

function messageOf(err: unknown): string {
  if (err instanceof Error) return err.message;
  return '操作失败，请稍后重试';
}

onMounted(() => {
  void load();
  void loadDatasets();
});
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row ev-head">
      <div>
        <h1 class="ap-title">引擎</h1>
        <p class="ap-page__desc">
          指标注册表（配置即数据）· changelog <b class="ap-mono">{{ changelogN }}</b> 条
          · 引擎数据集缺失 <b class="ap-mono" data-test="ev-datasets-missing">{{ datasetsMissing }}</b> 个
        </p>
      </div>
      <ApButton data-test="ev-add" @click="openAdd">新增指标</ApButton>
    </div>

    <!-- 数据缺失提示条（新增/更新联动；引导前往数据页同步） -->
    <div v-if="dataMissing.length" class="ev-missing" data-test="ev-missing-bar">
      <p v-for="m in dataMissing" :key="m.dataset">
        <b>{{ m.dataset }}</b> 尚未同步 —— {{ m.hint }}
      </p>
      <RouterLink class="ev-missing__link" to="/data" data-test="ev-missing-goto">
        前往数据页触发同步 →
      </RouterLink>
      <button class="ev-missing__close" type="button" data-test="ev-missing-close" @click="dataMissing = []">×</button>
    </div>

    <!-- 三引擎描述卡 -->
    <div class="ap-row ev-descs">
      <ApCard
        v-for="d in describes"
        :key="d.id"
        class="ev-desc"
        :class="{ 'is-active': activeEngine === d.id }"
        :title="ENGINE_LABEL[d.id] ?? d.name"
      >
        <button class="ev-desc__btn" type="button" :data-test="`ev-desc-${d.id}`" @click="switchEngine(d.id)">
          <p class="ev-desc__nums">
            指标 <b class="ap-mono">{{ d.metric_count }}</b> · 活跃
            <b class="ap-mono">{{ d.active_count }}</b>
          </p>
          <p class="ap-muted">{{ d.kind }}</p>
        </button>
      </ApCard>
    </div>

    <ApCard :title="`${ENGINE_LABEL[activeEngine] ?? activeEngine} · 指标清单`">
      <template #extra>
        <select v-model="statusFilter" class="ev-select" data-test="ev-status-filter" @change="load">
          <option value="">全部状态</option>
          <option value="active">active</option>
          <option value="inactive">inactive</option>
          <option value="experimental">experimental</option>
        </select>
      </template>

      <p v-if="loading" class="ap-muted" data-test="ev-loading">加载中…</p>
      <p v-else-if="error" class="ev-error" data-test="ev-error">{{ error }}</p>
      <ApTable
        v-else
        :columns="columns"
        :rows="rows"
        row-key="id"
        density="compact"
        empty-text="暂无指标"
        data-test="ev-table"
      >
        <template #cell:id="{ row }">
          <b>{{ row.id }}</b>
          <p v-if="row.note" class="ap-muted ev-note">{{ row.note }}</p>
        </template>
        <template #cell:formula="{ row }">
          <span class="ap-mono">{{ row.formula }}</span>
        </template>
        <template #cell:status="{ row }">
          <ApBadge :tone="statusTone(String(row.status))">{{ row.status }}</ApBadge>
        </template>
        <template #cell:actions="{ row }">
          <span class="ev-actions">
            <ApButton size="sm" variant="ghost" :data-test="`ev-edit-${row.id}`" @click="openEdit(row as unknown as EngineMetric)">编辑</ApButton>
            <ApButton size="sm" variant="ghost" :data-test="`ev-toggle-${row.id}`" @click="toggleStatus(row as unknown as EngineMetric)">
              {{ row.status === 'active' ? '停用' : '启用' }}
            </ApButton>
            <ApButton
              size="sm" variant="danger"
              :data-test="`ev-remove-${row.id}`"
              @click="removeTarget = row as unknown as EngineMetric; removeForm = { hypothesis_id: '', reason: '' }; removeOpen = true"
            >
              删除
            </ApButton>
          </span>
        </template>
      </ApTable>
    </ApCard>

    <ApCard title="引擎数据集状态">
      <p class="ap-muted ev-note">
        指标声明引用的数据集与落盘状态（与数据页「引擎数据集」卡片同源；同步后自动刷新需手动刷新页面）。
      </p>
      <ul class="ev-ds" data-test="ev-datasets">
        <li v-for="d in engineDatasets" :key="d.dataset" class="ev-ds__item" :data-test="`ev-dataset-${d.dataset}`">
          <ApBadge :tone="d.synced ? 'success' : 'danger'">{{ d.synced ? '已同步' : '未同步' }}</ApBadge>
          <b class="ap-mono">{{ d.dataset }}</b>
          <span class="ap-muted">
            as_of {{ d.as_of ? fmtDate(d.as_of) : DASH }} · {{ d.series }} 序列 ·
            {{ d.metrics.slice(0, 3).join(', ') }}{{ d.metrics.length > 3 ? '…' : '' }} 引用
          </span>
        </li>
      </ul>
      <p v-if="!engineDatasets.length" class="ap-muted">暂无数据集引用（alpha/beta 引擎走既有行情财务管道）。</p>
    </ApCard>

    <!-- 新增弹窗 -->
    <ApModal v-model="addOpen" title="新增指标">
      <div class="ev-form" data-test="ev-add-modal">
        <p class="ap-muted ev-note">
          引擎 <b>{{ ENGINE_LABEL[activeEngine] }}</b> · formula 须为内置清单（如 builtin:macro_series）；
          hypothesis_id 为研究门禁必填。
        </p>
        <label>指标 id *<input v-model="addForm.id" data-test="ev-add-id" placeholder="如 m2_yoy_v2" /></label>
        <label>公式 *<input v-model="addForm.formula" data-test="ev-add-formula" placeholder="builtin:macro_series" /></label>
        <label>hypothesis_id *<input v-model="addForm.hypothesis_id" data-test="ev-add-hypo" placeholder="H-XXXX" /></label>
        <template v-if="activeEngine === 'macro'">
          <label>维度（D1~D4）<input v-model="addForm.dim" placeholder="D1" /></label>
          <label>市场（A/HK/US）<input v-model="addForm.market" placeholder="A" /></label>
          <label>数据集<input v-model="addForm.dataset" placeholder="macro_liquidity" /></label>
          <label>scorer 类型<input v-model="addForm.scorerType" placeholder="step / quant / mom / clip / pulse" /></label>
          <label>scorer 参数<input v-model="addForm.scorerArg" placeholder='[8.0, 10.0] 或单值' /></label>
        </template>
        <label>备注<input v-model="addForm.note" placeholder="研究假设摘要" /></label>
        <label>变更原因<input v-model="addForm.reason" placeholder="登记进 changelog" /></label>
      </div>
      <template #footer>
        <ApButton variant="ghost" data-test="ev-add-cancel" @click="addOpen = false">取消</ApButton>
        <ApButton :loading="submitting" data-test="ev-add-submit" @click="submitAdd">提交</ApButton>
      </template>
    </ApModal>

    <!-- 编辑弹窗 -->
    <ApModal v-model="editOpen" :title="editTarget ? `编辑指标 · ${editTarget.id}` : '编辑指标'">
      <div class="ev-form" data-test="ev-edit-modal">
        <p class="ap-muted ev-note">version 将自增；params/scorer 未在此面编辑（走 CLI/MCP 深合并）。</p>
        <template v-if="activeEngine === 'macro'">
          <label>维度<input v-model="editForm.dim" /></label>
          <label>数据集<input v-model="editForm.dataset" /></label>
        </template>
        <label>备注<input v-model="editForm.note" /></label>
        <label>变更原因<input v-model="editForm.reason" /></label>
      </div>
      <template #footer>
        <ApButton variant="ghost" data-test="ev-edit-cancel" @click="editOpen = false">取消</ApButton>
        <ApButton :loading="submitting" data-test="ev-edit-submit" @click="submitEdit">保存</ApButton>
      </template>
    </ApModal>

    <!-- 软删确认弹窗 -->
    <ApModal v-model="removeOpen" :title="removeTarget ? `软删指标 · ${removeTarget.id}` : '软删指标'">
      <div class="ev-form" data-test="ev-remove-modal">
        <p class="ap-muted ev-note">
          软删 = status→inactive，条目与 changelog 保留（可 update 复活）；评估面即刻不再采用该指标。
        </p>
        <label>hypothesis_id *<input v-model="removeForm.hypothesis_id" data-test="ev-remove-hypo" /></label>
        <label>原因<input v-model="removeForm.reason" /></label>
      </div>
      <template #footer>
        <ApButton variant="ghost" data-test="ev-remove-cancel" @click="removeOpen = false">取消</ApButton>
        <ApButton variant="danger" :loading="submitting" data-test="ev-remove-submit" @click="submitRemove">确认软删</ApButton>
      </template>
    </ApModal>
  </main>
</template>

<style scoped>
.ev-head {
  justify-content: space-between;
  align-items: flex-start;
}
.ev-missing {
  position: relative;
  margin-bottom: var(--ap-sp-4);
  padding: 10px 36px 10px 12px;
  border: 1px solid rgba(180, 83, 9, 0.35);
  border-radius: var(--ap-radius);
  background: rgba(180, 83, 9, 0.08);
  color: #9a4708;
  font-size: var(--ap-fs-sm);
}
.ev-missing p {
  margin: 0 0 4px;
}
.ev-missing__link {
  font-weight: 600;
  color: inherit;
  text-decoration: underline;
}
.ev-missing__close {
  position: absolute;
  top: 6px;
  right: 8px;
  appearance: none;
  border: 0;
  background: transparent;
  font-size: 16px;
  color: inherit;
  cursor: pointer;
}
.ev-descs {
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  margin-bottom: var(--ap-sp-4);
}
.ev-desc {
  flex: 1 1 200px;
  cursor: pointer;
  border: 1px solid transparent;
}
.ev-desc.is-active {
  border-color: var(--ap-seal);
}
.ev-desc__btn {
  appearance: none;
  border: 0;
  background: transparent;
  padding: 0;
  width: 100%;
  text-align: left;
  cursor: pointer;
  color: inherit;
  font: inherit;
}
.ev-desc__nums {
  margin: 0 0 4px;
}
.ev-note {
  font-size: var(--ap-fs-sm);
  margin: 2px 0 0;
}
.ev-error {
  color: var(--ap-danger);
}
.ev-select {
  height: 28px;
  padding: 0 var(--ap-sp-2);
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  color: var(--ap-ink);
  font-size: var(--ap-fs-sm);
}
.ev-actions {
  display: inline-flex;
  gap: var(--ap-sp-1);
  flex-wrap: wrap;
}
.ev-ds {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
}
.ev-ds__item {
  display: flex;
  align-items: center;
  gap: var(--ap-sp-3);
  flex-wrap: wrap;
  padding: 7px 0;
  border-bottom: 1px solid var(--ap-hair);
  font-size: var(--ap-fs-sm);
}
.ev-ds__item:last-child {
  border-bottom: 0;
}
.ev-form {
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-3);
}
.ev-form label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
.ev-form input {
  height: 32px;
  padding: 0 var(--ap-sp-2);
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  color: var(--ap-ink);
  font-size: var(--ap-fs-sm);
}
</style>
