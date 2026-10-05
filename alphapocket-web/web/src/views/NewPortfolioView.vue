<script setup lang="ts">
// 新建组合向导（T6.3）：① 基本信息 → ② 标的与权重 → ③ 确认。
// 权重窗口冻结为 [99.5, 100.5]，越界显示归一建议并阻断提交；422 统一错误体内联映射。
import { computed, onBeforeUnmount, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';

import { createPortfolio, registerSecurity, searchSecurities } from '../api';
import { ApiError } from '../api/types';
import type { AssetType, Security } from '../api/types';
import { ApBadge, ApButton, ApCard, ApStepper, useToast } from '../components/ui';
import type { ApStepItem, BadgeTone } from '../components/ui';

const router = useRouter();
const toast = useToast();

const steps: ApStepItem[] = [
  { key: 'basic', label: '基本信息', hint: '名称 / pid' },
  { key: 'positions', label: '标的与权重', hint: '搜索 / 等权 / 归一' },
  { key: 'confirm', label: '确认', hint: '可选立即同步' },
];

interface EditPosition {
  secid: string;
  name: string;
  asset_type: AssetType;
  weight: number | null;
}

const TYPE_TONE: Record<AssetType, BadgeTone> = { STOCK: 'info', ETF: 'success', LOF: 'warn' };
const typeTone = (t: AssetType): BadgeTone => TYPE_TONE[t] ?? 'default';

/* ── 步骤与表单状态 ──────────────────────────────── */
const step = ref(0);
const name = ref('');
const note = ref('');
const pid = ref('');
const positions = ref<EditPosition[]>([]);
const syncNow = ref(false);
const submitting = ref(false);

const errors = reactive({ name: '', positions: '', weights: '', form: '' });

/* ── 标的搜索（防抖） ────────────────────────────── */
const SEARCH_DEBOUNCE = 300;
const searchQuery = ref('');
const searchResults = ref<Security[]>([]);
const searching = ref(false);
let searchTimer: ReturnType<typeof setTimeout> | undefined;

function onSearchInput(): void {
  if (searchTimer) clearTimeout(searchTimer);
  const q = searchQuery.value.trim();
  if (!q) {
    searchResults.value = [];
    searching.value = false;
    return;
  }
  searching.value = true;
  searchTimer = setTimeout(() => void runSearch(q), SEARCH_DEBOUNCE);
}

async function runSearch(q: string): Promise<void> {
  try {
    const list = await searchSecurities(q);
    // 仅当查询词未变化时落地，避免竞态覆盖
    if (searchQuery.value.trim() === q) searchResults.value = list;
  } catch (err) {
    errors.positions = messageOf(err);
  } finally {
    searching.value = false;
  }
}

const isAdded = (secid: string): boolean => positions.value.some((p) => p.secid === secid);

function addPosition(sec: Security): void {
  if (isAdded(sec.secid)) {
    errors.positions = `${sec.name || sec.code} 已在组合中`;
    return;
  }
  errors.positions = '';
  positions.value.push({
    secid: sec.secid,
    name: sec.name || sec.code,
    asset_type: sec.asset_type,
    weight: null,
  });
}

/* ── 新标的即时注册 ──────────────────────────────── */
const registerCode = ref('');
const registerType = ref<AssetType>('STOCK');
const registering = ref(false);

async function registerNew(): Promise<void> {
  const code = registerCode.value.trim();
  if (!code) {
    errors.positions = '请输入标的代码';
    return;
  }
  registering.value = true;
  errors.positions = '';
  try {
    const sec = await registerSecurity(code, registerType.value);
    addPosition(sec);
    registerCode.value = '';
    toast.success(`已注册 ${sec.secid}`);
  } catch (err) {
    errors.positions = messageOf(err);
  } finally {
    registering.value = false;
  }
}

/* ── 权重编辑器 ──────────────────────────────────── */
const weightSum = computed(() =>
  positions.value.reduce((sum, p) => sum + (Number(p.weight) || 0), 0),
);
const hasNonPositiveWeight = computed(() => positions.value.some((p) => !(Number(p.weight) > 0)));
const weightsInWindow = computed(() => weightSum.value >= 99.5 && weightSum.value <= 100.5);
const positionsValid = computed(
  () => positions.value.length > 0 && !hasNonPositiveWeight.value && weightsInWindow.value,
);

/** 等权一键：按 100/n 分配（保留 1 位小数，便于展示 20.0）。 */
function applyEqualWeight(): void {
  const n = positions.value.length;
  if (!n) return;
  const w = Number((100 / n).toFixed(1));
  positions.value.forEach((p) => {
    p.weight = w;
  });
}

function removePosition(index: number): void {
  positions.value.splice(index, 1);
}

/** 按比例归一到 100 的建议值（sum>0 时给出）。 */
const normalizeSuggestion = computed(() => {
  const sum = weightSum.value;
  if (sum <= 0) return [] as { label: string; weight: number }[];
  return positions.value.map((p) => ({
    label: p.name || p.secid,
    weight: Number((((Number(p.weight) || 0) * 100) / sum).toFixed(2)),
  }));
});

const normalizeHint = computed(() => {
  if (positions.value.length === 0 || weightsInWindow.value) return '';
  const parts = normalizeSuggestion.value.map((s) => `${s.label} ${s.weight.toFixed(2)}`).join(' / ');
  return `权重和为 ${weightSum.value.toFixed(1)}%，超出 [99.5, 100.5]；建议按比例归一到 100：${parts}`;
});

/* ── 错误处理（统一错误体内联映射） ────────────────── */
function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return '操作失败，请稍后重试';
}

function suggestionOf(detail: unknown): string {
  if (!detail || typeof detail !== 'object') return '';
  const s = (detail as Record<string, unknown>).suggestion;
  if (!s) return '';
  if (typeof s === 'string') return s;
  if (Array.isArray(s)) {
    return s
      .map((v) => {
        if (v && typeof v === 'object') {
          const o = v as Record<string, unknown>;
          const w = Number(o.weight);
          const label = o.secid ?? o.name ?? '';
          return `${label} ${Number.isFinite(w) ? w.toFixed(2) : ''}`.trim();
        }
        const n = Number(v);
        return Number.isFinite(n) ? n.toFixed(2) : String(v);
      })
      .join(' / ');
  }
  return String(s);
}

function applyApiError(err: unknown): void {
  const code = err instanceof ApiError ? err.code : '';
  const detail = err instanceof ApiError ? err.detail : undefined;
  const msg = messageOf(err);
  const sug = suggestionOf(detail);
  const withSug = sug ? `${msg}（归一建议：${sug}）` : msg;

  if (code === 'WEIGHT_NOT_NORMALIZED') {
    errors.weights = withSug;
    step.value = 1;
  } else if (code === 'SECURITY_NOT_FOUND') {
    const badCode =
      detail && typeof detail === 'object' ? String((detail as Record<string, unknown>).code ?? '') : '';
    const target = positions.value.find(
      (p) => badCode && (p.secid === badCode || p.secid.startsWith(badCode)),
    );
    errors.positions = target ? `${target.name || target.secid}：${msg}` : msg;
    step.value = 1;
  } else if (code === 'VALIDATION_ERROR') {
    if (/name|名称/i.test(msg)) {
      errors.name = msg;
      step.value = 0;
    } else {
      errors.form = msg;
    }
  } else {
    errors.form = msg;
  }
}

/* ── 步骤流转 ────────────────────────────────────── */
function goToStep(index: number): void {
  step.value = index;
}

function toPositions(): void {
  if (!name.value.trim()) {
    errors.name = '请填写组合名称';
    return;
  }
  errors.name = '';
  step.value = 1;
}

function toConfirm(): void {
  if (!positionsValid.value) return;
  errors.weights = '';
  step.value = 2;
}

function resetErrors(): void {
  errors.name = '';
  errors.positions = '';
  errors.weights = '';
  errors.form = '';
}

async function submit(): Promise<void> {
  resetErrors();
  if (!name.value.trim()) {
    errors.name = '请填写组合名称';
    step.value = 0;
    return;
  }
  if (!positionsValid.value) {
    errors.weights = '权重和需在 [99.5, 100.5] 区间内，请按提示归一后再提交';
    step.value = 1;
    return;
  }
  submitting.value = true;
  try {
    const created = await createPortfolio({
      name: name.value.trim(),
      pid: pid.value.trim() || undefined,
      note: note.value.trim() || undefined,
      positions: positions.value.map((p) => ({ secid: p.secid, weight: Number(p.weight) })),
      equal_weight: false,
    });
    toast.success(`组合「${created.name}」已创建`);
    await router.push(syncNow.value ? '/data' : `/portfolios/${created.pid}`);
  } catch (err) {
    applyApiError(err);
  } finally {
    submitting.value = false;
  }
}

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer);
});
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head">
      <h1 class="ap-title">新建组合</h1>
      <p class="ap-page__desc">三步向导：基本信息 → 标的与权重 → 确认。</p>
    </div>

    <ApCard>
      <ApStepper :steps="steps" :current="step" @change="goToStep" />

      <!-- 步骤① 基本信息 -->
      <section v-if="step === 0" class="np-body ap-stack" data-test="step-basic">
        <label class="np-field">
          <span class="np-label">组合名称 *</span>
          <input
            v-model="name"
            class="np-input"
            type="text"
            placeholder="如：沪深300/南方原油/黄金/纳指/十年国债"
            data-test="name"
          />
          <span v-if="errors.name" class="np-error" role="alert" data-test="name-error">
            {{ errors.name }}
          </span>
        </label>

        <label class="np-field">
          <span class="np-label">备注</span>
          <input v-model="note" class="np-input" type="text" placeholder="可选" data-test="note" />
        </label>

        <label class="np-field">
          <span class="np-label">组合短码 pid</span>
          <input
            v-model="pid"
            class="np-input ap-mono"
            type="text"
            placeholder="留空自动生成"
            data-test="pid"
          />
        </label>

        <div class="np-actions">
          <ApButton variant="primary" data-test="to-positions" @click="toPositions">下一步</ApButton>
        </div>
      </section>

      <!-- 步骤② 标的与权重 -->
      <section v-else-if="step === 1" class="np-body" data-test="step-positions">
        <div class="np-block ap-stack">
          <span class="np-label">搜索标的</span>
          <input
            v-model="searchQuery"
            class="np-input"
            type="text"
            placeholder="输入代码或名称前缀（防抖检索）"
            data-test="search-input"
            @input="onSearchInput"
          />
          <p v-if="searching" class="ap-muted" data-test="searching">搜索中…</p>
          <ul v-if="searchResults.length" class="np-results" data-test="search-results">
            <li v-for="sec in searchResults" :key="sec.secid" class="np-result">
              <span class="ap-mono">{{ sec.secid }}</span>
              <span class="np-result__name">{{ sec.name }}</span>
              <ApBadge :tone="typeTone(sec.asset_type)">{{ sec.asset_type }}</ApBadge>
              <ApButton
                size="sm"
                variant="ghost"
                :disabled="isAdded(sec.secid)"
                data-test="search-result"
                @click="addPosition(sec)"
              >
                {{ isAdded(sec.secid) ? '已添加' : '添加' }}
              </ApButton>
            </li>
          </ul>
        </div>

        <div class="np-block ap-stack">
          <span class="np-label">注册新标的（即时 register）</span>
          <div class="np-register ap-row">
            <input
              v-model="registerCode"
              class="np-input ap-mono"
              type="text"
              placeholder="6 位代码，如 510300"
              data-test="register-code"
            />
            <select v-model="registerType" class="np-input np-select" data-test="register-type">
              <option value="STOCK">STOCK</option>
              <option value="ETF">ETF</option>
              <option value="LOF">LOF</option>
            </select>
            <ApButton
              variant="ghost"
              :loading="registering"
              :disabled="registering"
              data-test="register-btn"
              @click="registerNew"
            >
              注册并添加
            </ApButton>
          </div>
          <span v-if="errors.positions" class="np-error" role="alert" data-test="positions-error">
            {{ errors.positions }}
          </span>
        </div>

        <div class="np-block ap-stack">
          <div class="np-block__head ap-row">
            <span class="np-label">持仓与权重</span>
            <ApButton variant="ghost" size="sm" data-test="equal-weight" @click="applyEqualWeight">
              等权一键
            </ApButton>
          </div>

          <p v-if="positions.length === 0" class="ap-muted" data-test="positions-empty">
            尚未添加标的：请搜索或注册。
          </p>

          <ul v-else class="np-poslist" data-test="position-list">
            <li v-for="(pos, index) in positions" :key="pos.secid" class="np-pos">
              <span class="ap-mono np-pos__secid">{{ pos.secid }}</span>
              <span class="np-pos__name">{{ pos.name }}</span>
              <ApBadge :tone="typeTone(pos.asset_type)">{{ pos.asset_type }}</ApBadge>
              <input
                v-model.number="pos.weight"
                class="np-input np-weight ap-mono"
                type="number"
                step="0.1"
                min="0"
                data-test="weight-input"
              />
              <button
                class="np-remove"
                type="button"
                aria-label="移除"
                data-test="remove-position"
                @click="removePosition(index)"
              >
                ×
              </button>
            </li>
          </ul>

          <p class="np-sum ap-mono" data-test="weight-sum">合计 {{ weightSum.toFixed(1) }}%</p>

          <p v-if="normalizeHint" class="np-hint" role="alert" data-test="normalize-hint">
            {{ normalizeHint }}
          </p>
          <p v-if="errors.weights" class="np-error" role="alert" data-test="weights-error">
            {{ errors.weights }}
          </p>
        </div>

        <div class="np-actions">
          <ApButton variant="ghost" data-test="back-basic" @click="goToStep(0)">上一步</ApButton>
          <ApButton
            variant="primary"
            :disabled="!positionsValid"
            data-test="to-confirm"
            @click="toConfirm"
          >
            下一步
          </ApButton>
        </div>
      </section>

      <!-- 步骤③ 确认 -->
      <section v-else class="np-body ap-stack" data-test="step-confirm">
        <div class="np-summary" data-test="summary">
          <div class="np-summary__row">
            <span class="ap-muted">名称</span><span data-test="summary-name">{{ name }}</span>
          </div>
          <div class="np-summary__row">
            <span class="ap-muted">pid</span>
            <span class="ap-mono" data-test="summary-pid">{{ pid.trim() || '自动生成' }}</span>
          </div>
          <div v-if="note.trim()" class="np-summary__row">
            <span class="ap-muted">备注</span><span>{{ note }}</span>
          </div>
          <ul class="np-summary__list">
            <li v-for="pos in positions" :key="pos.secid" class="np-summary__pos">
              <span class="ap-mono">{{ pos.secid }}</span>
              <span>{{ pos.name }}</span>
              <ApBadge :tone="typeTone(pos.asset_type)">{{ pos.asset_type }}</ApBadge>
              <span class="ap-mono">{{ Number(pos.weight).toFixed(2) }}%</span>
            </li>
          </ul>
          <div class="np-summary__row">
            <span class="ap-muted">权重合计</span>
            <span class="ap-mono" data-test="summary-weight-sum">{{ weightSum.toFixed(1) }}%</span>
          </div>
        </div>

        <label class="np-check">
          <input v-model="syncNow" type="checkbox" data-test="sync-now" />
          <span>立即同步数据（提交后跳转数据页跟踪 job）</span>
        </label>

        <p v-if="errors.form" class="np-error" role="alert" data-test="form-error">
          {{ errors.form }}
        </p>

        <div class="np-actions">
          <ApButton variant="ghost" data-test="back-positions" @click="goToStep(1)">上一步</ApButton>
          <ApButton
            variant="primary"
            :loading="submitting"
            :disabled="submitting || !positionsValid"
            data-test="submit"
            @click="submit"
          >
            创建组合
          </ApButton>
        </div>
      </section>
    </ApCard>
  </main>
</template>

<style scoped>
.np-body {
  margin-top: var(--ap-sp-5);
}
.np-block {
  padding: var(--ap-sp-3) 0;
  border-top: 1px solid var(--ap-hair);
}
.np-block__head {
  justify-content: space-between;
}
.np-field {
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-1);
}
.np-label {
  font-family: var(--ap-font-mono);
  font-size: var(--ap-fs-xs);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ap-mute);
}
.np-input {
  height: 34px;
  padding: 0 var(--ap-sp-3);
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  color: var(--ap-ink);
  font-family: var(--ap-font-sans);
  font-size: var(--ap-fs-base);
}
.np-input:focus {
  outline: none;
  border-color: var(--ap-seal);
  background: var(--ap-card);
  box-shadow: 0 0 0 3px var(--ap-seal-soft);
}
.np-select {
  width: 110px;
  flex: 0 0 auto;
}
.np-results,
.np-poslist,
.np-summary__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
}
.np-result,
.np-pos,
.np-summary__pos {
  display: flex;
  align-items: center;
  gap: var(--ap-sp-2);
  padding: 6px 0;
  border-bottom: 1px solid var(--ap-hair);
}
.np-result__name,
.np-pos__name {
  flex: 1;
  min-width: 0;
}
.np-weight {
  width: 88px;
  flex: 0 0 auto;
  text-align: right;
}
.np-remove {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--ap-mute);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}
.np-remove:hover {
  color: var(--ap-danger);
}
.np-sum {
  margin-top: var(--ap-sp-2);
  font-size: var(--ap-fs-sm);
  color: var(--ap-dim);
}
.np-hint {
  margin-top: var(--ap-sp-2);
  padding: 8px 10px;
  font-size: var(--ap-fs-sm);
  color: var(--ap-warn);
  background: rgba(176, 140, 60, 0.1);
  border: 1px solid rgba(176, 140, 60, 0.35);
  border-radius: var(--ap-radius-sm);
}
.np-error {
  display: block;
  margin-top: var(--ap-sp-2);
  padding: 7px 10px;
  font-size: var(--ap-fs-sm);
  color: var(--ap-danger);
  background: var(--ap-seal-soft);
  border: 1px solid var(--ap-seal-line);
  border-radius: var(--ap-radius-sm);
}
.np-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--ap-sp-2);
  margin-top: var(--ap-sp-4);
}
.np-summary {
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
  padding: var(--ap-sp-3);
  background: var(--ap-card2);
  border-radius: var(--ap-radius);
}
.np-summary__row {
  display: flex;
  gap: var(--ap-sp-3);
}
.np-summary__row > :first-child {
  width: 72px;
}
.np-check {
  display: flex;
  align-items: center;
  gap: var(--ap-sp-2);
  font-size: var(--ap-fs-sm);
}
</style>
