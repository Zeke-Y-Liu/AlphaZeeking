<script setup lang="ts">
// 组合报告页（T6.4）：最近一次 review run 的 HTML 报告以鉴权取回后绑定 iframe srcdoc，
// 并提供 report.md / review.json 的下载（鉴权取 blob → createObjectURL）。
import { computed, onMounted, ref } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import { fetchRunArtifact, getPortfolio, listRuns } from '../api';
import type { Portfolio, Run } from '../api/types';
import { ApBadge, ApButton, ApCard, useToast } from '../components/ui';

const route = useRoute();
const toast = useToast();

const pfId = computed(() => String(route.params.id));

const pf = ref<Portfolio | null>(null);
const run = ref<Run | null>(null);
const loading = ref(false);
const error = ref('');
const html = ref('');
const htmlError = ref('');

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  html.value = '';
  htmlError.value = '';
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
    if (run.value) {
      try {
        html.value = await fetchRunArtifact<string>(run.value.run_uid, 'report.html', 'text');
      } catch {
        htmlError.value = 'HTML 报告产物不可读或尚未生成。';
      }
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '报告加载失败';
  } finally {
    loading.value = false;
  }
}

/** 鉴权取产物 → 触发浏览器下载。 */
async function download(file: string): Promise<void> {
  if (!run.value) return;
  try {
    const blob = await fetchRunArtifact<Blob>(run.value.run_uid, file, 'blob');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = file;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch {
    toast.error(`下载 ${file} 失败`);
  }
}

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row rp-head">
      <div>
        <h1 class="ap-title">{{ pf?.name ?? `组合报告 · ${pfId}` }}</h1>
        <p class="ap-page__desc ap-row rp-desc">
          <ApBadge tone="info" data-test="rp-asof">数据截至 {{ run?.as_of ?? '—' }}</ApBadge>
          <span class="ap-muted ap-mono" data-test="rp-run">{{ run?.run_uid ?? '—' }}</span>
        </p>
      </div>
      <RouterLink :to="`/portfolios/${pfId}`">
        <ApButton variant="ghost" data-test="rp-back">返回详情</ApButton>
      </RouterLink>
    </div>

    <p v-if="loading" class="ap-muted" data-test="rp-loading">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p class="rp-error" role="alert" data-test="rp-error">{{ error }}</p>
      <ApButton variant="ghost" data-test="rp-retry" @click="load">重试</ApButton>
    </ApCard>

    <ApCard v-else-if="!run" title="报告">
      <p class="ap-muted" data-test="rp-empty">该组合尚无评估记录，暂无报告可查看。</p>
      <RouterLink :to="`/portfolios/${pfId}`">
        <ApButton class="rp-mt" variant="ghost" data-test="rp-back-empty">返回详情页运行评估</ApButton>
      </RouterLink>
    </ApCard>

    <div v-else class="ap-stack">
      <div class="rp-dl ap-row">
        <a href="#" class="rp-link" data-test="dl-md" @click.prevent="download('report.md')">下载 report.md</a>
        <a href="#" class="rp-link" data-test="dl-json" @click.prevent="download('review.json')">
          下载 review.json
        </a>
      </div>

      <ApCard title="HTML 报告">
        <p v-if="htmlError" class="ap-muted" data-test="rp-html-error">{{ htmlError }}</p>
        <iframe
          v-else
          class="rp-frame"
          data-test="report-frame"
          sandbox="allow-same-origin"
          :srcdoc="html"
          title="组合评估报告"
        />
      </ApCard>
    </div>
  </main>
</template>

<style scoped>
.rp-head {
  justify-content: space-between;
  align-items: flex-start;
}
.rp-desc {
  flex-wrap: wrap;
}
.rp-error {
  color: var(--ap-danger);
  margin-bottom: var(--ap-sp-3);
}
.rp-mt {
  margin-top: var(--ap-sp-3);
}
.rp-dl {
  gap: var(--ap-sp-4);
  flex-wrap: wrap;
}
.rp-link {
  color: var(--ap-seal);
  font-size: var(--ap-fs-sm);
  text-decoration: underline;
}
.rp-frame {
  display: block;
  width: 100%;
  height: 78vh;
  min-height: 480px;
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  background: #fff;
}
</style>
