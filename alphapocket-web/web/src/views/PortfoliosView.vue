<script setup lang="ts">
// 组合列表页（T6.3）：名称 / pid / 标的徽章行 / 最近评估摘要四指标 / 最近 run 时间 / 状态。
import { onMounted, ref } from 'vue';

import { listPortfolios } from '../api';
import type { AssetType, Portfolio } from '../api/types';
import { ApBadge, ApButton, ApCard } from '../components/ui';
import type { BadgeTone } from '../components/ui';
import { DASH, fmtDate, fmtPct } from '../utils/format';

const items = ref<Portfolio[]>([]);
const loading = ref(false);
const error = ref('');

/** asset_type → 徽章色档。 */
const TYPE_TONE: Record<AssetType, BadgeTone> = { STOCK: 'info', ETF: 'success', LOF: 'warn' };

function typeTone(t?: AssetType | null): BadgeTone {
  return t ? (TYPE_TONE[t] ?? 'default') : 'default';
}

/** 最近评估摘要四指标（当日·YTD·1年·MDD）；core latest_run.summary.portfolio_metrics。 */
function metrics(pf: Portfolio): { key: string; label: string; value: string }[] {
  const m = pf.latest_run?.summary?.portfolio_metrics;
  return [
    { key: 'day', label: '当日', value: fmtPct(m?.day_ret) },
    { key: 'ytd', label: 'YTD', value: fmtPct(m?.ret_ytd) },
    { key: '1y', label: '1年', value: fmtPct(m?.ret_1y) },
    { key: 'mdd', label: 'MDD', value: fmtPct(m?.max_dd_1y) },
  ];
}

function runTime(pf: Portfolio): string {
  return fmtDate(pf.latest_run?.as_of);
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    items.value = await listPortfolios();
  } catch (err) {
    error.value = err instanceof Error ? err.message : '组合列表加载失败';
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row">
      <div>
        <h1 class="ap-title">组合</h1>
        <p class="ap-page__desc">组合卡片列表（PC 三栏中栏 / H5 tabbar「组合」）。</p>
      </div>
      <RouterLink to="/portfolios/new">
        <ApButton variant="primary" data-test="new-portfolio">新建组合</ApButton>
      </RouterLink>
    </div>

    <p v-if="loading" class="ap-muted" data-test="pf-loading">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p class="pf-error" role="alert" data-test="pf-error">{{ error }}</p>
      <ApButton variant="ghost" data-test="pf-retry" @click="load">重试</ApButton>
    </ApCard>

    <ApCard v-else-if="items.length === 0" title="列表">
      <p class="ap-muted" data-test="pf-empty">暂无组合，点击右上角「新建组合」开始。</p>
    </ApCard>

    <div v-else class="pf-grid">
      <ApCard v-for="pf in items" :key="pf.pid" class="pf-card" data-test="pf-card">
        <div class="pf-head">
          <div class="pf-head__txt">
            <RouterLink class="pf-name" :to="`/portfolios/${pf.pid}`" data-test="pf-name">
              {{ pf.name }}
            </RouterLink>
            <span class="ap-mono ap-muted" data-test="pf-pid">{{ pf.pid }}</span>
          </div>
          <ApBadge :tone="pf.status === 'active' ? 'success' : 'default'" data-test="pf-status">
            {{ pf.status }}
          </ApBadge>
        </div>

        <div class="pf-seclist" data-test="pf-seclist">
          <template v-if="pf.positions && pf.positions.length">
            <span v-for="pos in pf.positions" :key="pos.secid" class="pf-sec">
              <span class="ap-mono">{{ pos.name || pos.secid }}</span>
              <ApBadge v-if="pos.asset_type" :tone="typeTone(pos.asset_type)">
                {{ pos.asset_type }}
              </ApBadge>
            </span>
          </template>
          <span v-else class="ap-muted" data-test="pf-position-count">
            标的 {{ pf.position_count ?? 0 }} 只
          </span>
        </div>

        <div class="pf-metrics" data-test="pf-metrics">
          <div v-for="m in metrics(pf)" :key="m.key" class="pf-metric">
            <span class="pf-metric__lbl">{{ m.label }}</span>
            <span class="ap-mono pf-metric__val" :data-test="`pf-metric-${m.key}`">{{ m.value }}</span>
          </div>
        </div>

        <div class="pf-foot">
          <span class="ap-muted" data-test="pf-run-time">最近 run：{{ runTime(pf) }}</span>
          <span class="ap-muted ap-mono" data-test="pf-run-type">{{ pf.latest_run?.type ?? DASH }}</span>
        </div>
      </ApCard>
    </div>
  </main>
</template>

<style scoped>
.pf-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--ap-sp-4);
}
.pf-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--ap-sp-2);
}
.pf-head__txt {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.pf-name {
  font-family: var(--ap-font-serif);
  font-size: var(--ap-fs-md);
  font-weight: 600;
}
.pf-name:hover {
  color: var(--ap-seal);
}
.pf-seclist {
  display: flex;
  flex-wrap: wrap;
  gap: var(--ap-sp-2);
  margin-top: var(--ap-sp-3);
}
.pf-sec {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--ap-fs-sm);
}
.pf-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--ap-sp-2);
  margin-top: var(--ap-sp-3);
  padding: var(--ap-sp-3) 0;
  border-top: 1px solid var(--ap-hair);
  border-bottom: 1px solid var(--ap-hair);
}
.pf-metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.pf-metric__lbl {
  font-family: var(--ap-font-mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--ap-mute);
}
.pf-metric__val {
  font-size: var(--ap-fs-sm);
}
.pf-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-sp-2);
  margin-top: var(--ap-sp-3);
  font-size: 11px;
}
.pf-error {
  color: var(--ap-danger);
  margin-bottom: var(--ap-sp-3);
}
</style>
