<script setup lang="ts">
// 单公司分析列表（M10）：档案卡片 + 新建（secid）+ 删除（软删，级联归档影子组合）。
import { computed, onMounted, ref } from 'vue';

import { createAnalysis, deleteAnalysis, listAnalyses } from '../api';
import type { AnalysisProfile } from '../api/types';
import { ApBadge, ApButton, ApCard, useToast } from '../components/ui';
import { DASH, fmtDate } from '../utils/format';

const items = ref<AnalysisProfile[]>([]);
const loading = ref(false);
const error = ref('');
const creating = ref(false);
const secid = ref('');
const name = ref('');
const toast = useToast();

const MODULE_LABELS: Record<string, string> = {
  fundamental: '基本面', industry: '行业', technical: '技术', news: '消息',
};

function modulesOf(p: AnalysisProfile): { key: string; label: string }[] {
  return Object.keys(MODULE_LABELS)
    .filter((k) => p.modules?.[k])
    .map((k) => ({ key: k, label: MODULE_LABELS[k] }));
}

function summaryText(p: AnalysisProfile): string {
  const s = (p.latest_run?.summary ?? {}) as Record<string, unknown>;
  if (!p.latest_run) return '尚未运行分析';
  const os = s.oversold;
  return [
    `as_of ${fmtDate(p.latest_run.as_of ?? null)}`,
    os === true ? '短线超卖' : os === false ? '未超卖' : DASH,
    s.industry_board ? `行业 ${String(s.industry_board)}` : '',
  ].filter(Boolean).join(' · ');
}

const canCreate = computed(() => secid.value.trim().length > 0 && !creating.value);

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    items.value = await listAnalyses();
  } catch (err) {
    error.value = err instanceof Error ? err.message : '分析档案加载失败';
  } finally {
    loading.value = false;
  }
}

async function create(): Promise<void> {
  if (!canCreate.value) return;
  creating.value = true;
  try {
    const p = await createAnalysis({
      secid: secid.value.trim().toUpperCase(),
      name: name.value.trim() || undefined,
    });
    toast.success(`已创建 ${p.aid}（影子组合 ${p.shadow_pid}）`);
    secid.value = '';
    name.value = '';
    await load();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '创建失败');
  } finally {
    creating.value = false;
  }
}

async function remove(p: AnalysisProfile): Promise<void> {
  if (typeof window !== 'undefined' && !window.confirm(`确认删除分析档案 ${p.aid}？（影子组合将归档）`)) {
    return;
  }
  try {
    await deleteAnalysis(p.aid);
    toast.success(`已归档 ${p.aid}`);
    await load();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '删除失败');
  }
}

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head">
      <div>
        <h1 class="ap-title">单公司分析</h1>
        <p class="ap-page__desc">
          基本面 / 行业趋势 / 短线超卖 / 最新消息；档案自带单标的影子组合，可直接回测。
        </p>
      </div>
    </div>

    <ApCard title="新建分析档案">
      <form class="co-form" @submit.prevent="create">
        <input
          v-model="secid"
          class="co-input co-form__secid"
          placeholder="证券代码（如 601058.SH / 00700.HK / AAPL.US）"
          data-test="co-secid"
        />
        <input v-model="name" class="co-input co-form__name" placeholder="档案名（可选）" />
        <ApButton variant="primary" type="submit" :disabled="!canCreate" data-test="co-create">
          {{ creating ? '创建中…' : '创建' }}
        </ApButton>
      </form>
      <p class="ap-muted co-hint">
        标的需要先注册（数据页可注册标的）；创建时自动生成 <code>shadow_&lt;aid&gt;</code> 影子组合。
      </p>
    </ApCard>

    <p v-if="loading" class="ap-muted" data-test="co-loading">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p class="co-error" role="alert">{{ error }}</p>
      <ApButton variant="ghost" @click="load">重试</ApButton>
    </ApCard>

    <ApCard v-else-if="items.length === 0" title="列表">
      <p class="ap-muted" data-test="co-empty">暂无分析档案，先在上方创建。</p>
    </ApCard>

    <div v-else class="co-grid">
      <ApCard v-for="p in items" :key="p.aid" class="co-card" data-test="co-card">
        <div class="co-head">
          <div>
            <RouterLink class="co-name" :to="`/companies/${p.aid}`" data-test="co-name">
              {{ p.name }}
            </RouterLink>
            <div class="ap-mono ap-muted">{{ p.aid }} · {{ p.secid }}</div>
          </div>
          <ApBadge :tone="p.status === 'active' ? 'success' : 'default'">{{ p.status }}</ApBadge>
        </div>

        <div class="co-mods">
          <ApBadge v-for="m in modulesOf(p)" :key="m.key" tone="info">{{ m.label }}</ApBadge>
          <span v-if="modulesOf(p).length === 0" class="ap-muted">模块全关</span>
        </div>

        <p class="ap-muted co-summary">{{ summaryText(p) }}</p>

        <div class="co-foot">
          <RouterLink :to="`/portfolios/${p.shadow_pid}/backtest`">
            <ApButton variant="ghost" size="sm">回测</ApButton>
          </RouterLink>
          <ApButton variant="ghost" size="sm" @click="remove(p)" data-test="co-delete">删除</ApButton>
        </div>
      </ApCard>
    </div>
  </main>
</template>

<style scoped>
.co-form { display: flex; gap: var(--ap-sp-2); flex-wrap: wrap; }
.co-form__secid { flex: 1 1 320px; }
.co-form__name { flex: 1 1 200px; }
.co-input {
  height: 34px;
  padding: 0 var(--ap-sp-3);
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  color: var(--ap-ink);
  font-family: var(--ap-font-sans);
  font-size: var(--ap-fs-base);
}
.co-input:focus {
  outline: none;
  border-color: var(--ap-seal);
  background: var(--ap-card);
  box-shadow: 0 0 0 3px var(--ap-seal-soft);
}
.co-hint { margin-top: var(--ap-sp-2); font-size: var(--ap-fs-sm); }
.co-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: var(--ap-sp-3); }
.co-head { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--ap-sp-2); }
.co-name { font-weight: 600; font-size: var(--ap-fs-lg); }
.co-mods { display: flex; gap: var(--ap-sp-1); flex-wrap: wrap; margin: var(--ap-sp-2) 0; }
.co-summary { font-size: var(--ap-fs-sm); }
.co-foot { display: flex; justify-content: flex-end; gap: var(--ap-sp-1); }
.co-error { color: var(--ap-danger); }
</style>
