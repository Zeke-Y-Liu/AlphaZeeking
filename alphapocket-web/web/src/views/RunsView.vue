<script setup lang="ts">
// 运行历史（T6.7/M7-1）：type 筛选 chips + 运行表格（类型徽章 / run_uid / as_of / 时间 /
// 产物直达）+ 产物预览弹窗（json → pre，html → iframe srcdoc，其余下载）。
// 数据源 GET /v1/runs?type=&limit=（最新在前）；产物经 fetchRunArtifact 鉴权取回。
import { computed, onMounted, ref } from 'vue';

import { fetchRunArtifact, listRuns, runArtifactUrl } from '../api';
import type { Run } from '../api/types';
import { ApBadge, ApCard } from '../components/ui';

const TYPE_LABEL: Record<string, string> = {
  review: '评估',
  backtest: '回测',
  rebalance_plan: '调仓计划',
  exec_orders: '执行单',
  data_sync: '数据同步',
};
const FILTERS = [
  { key: '', label: '全部' },
  ...Object.entries(TYPE_LABEL).map(([key, label]) => ({ key, label })),
];

const runs = ref<Run[]>([]);
const loading = ref(false);
const error = ref('');
const filter = ref('');
const preview = ref<{ runUid: string; file: string; kind: 'json' | 'html' | 'text'; content: string } | null>(null);
const previewLoading = ref(false);

const rows = computed(() =>
  runs.value.map((r) => ({
    ...r,
    type_label: TYPE_LABEL[r.type] ?? r.type,
    created: (r.created_at || '').replace('T', ' ').slice(0, 19),
  })),
);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    runs.value = await listRuns({ type: filter.value || undefined, limit: 100 });
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

function pickFile(r: Run): string {
  const keys = r.artifact_keys || [];
  return keys.includes('report.html') ? 'report.html' : (keys[0] ?? '');
}

async function openArtifact(r: Run) {
  const file = pickFile(r);
  if (!file) return;
  const kind = file.endsWith('.html') ? 'html' : file.endsWith('.json') ? 'json' : 'text';
  preview.value = { runUid: r.run_uid, file, kind, content: '' };
  previewLoading.value = true;
  try {
    const res = await fetchRunArtifact<unknown>(r.run_uid, file, kind === 'json' ? 'json' : 'text');
    preview.value = {
      ...preview.value!,
      content: kind === 'json' ? JSON.stringify(res, null, 2) : String(res),
    };
  } catch {
    preview.value = { ...preview.value!, kind: 'text', content: '产物加载失败（可能尚未生成）' };
  } finally {
    previewLoading.value = false;
  }
}

function downloadArtifact(r: Run) {
  const file = pickFile(r);
  if (!file) return;
  window.open(runArtifactUrl(r.run_uid, file), '_blank');
}

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row">
      <div>
        <h1 class="ap-title">运行历史</h1>
        <p class="ap-page__desc">五类运行记录（最新在前）+ 产物直达。</p>
      </div>
    </div>

    <div class="ap-row ap-row--wrap ck-filters">
      <button
        v-for="f in FILTERS"
        :key="f.key"
        class="ck-chip"
        :class="{ 'ck-chip--active': filter === f.key }"
        type="button"
        @click="filter = f.key; load()"
      >
        {{ f.label }}
      </button>
    </div>

    <ApCard>
      <p v-if="error" class="ap-muted">加载失败：{{ error }}</p>
      <p v-else-if="loading && !runs.length" class="ap-muted">加载中…</p>
      <p v-else-if="!rows.length" class="ap-muted">暂无运行记录</p>
      <template v-else>
        <!-- 表格：窄屏横向滚动（列较多，与 ApTable 的 wrap 行为一致） -->
        <div class="ck-table-scroll">
          <table class="ck-table">
            <thead>
              <tr>
                <th>类型</th>
                <th>Run</th>
                <th>as_of</th>
                <th>时间</th>
                <th>产物</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in rows" :key="r.run_uid">
                <td><ApBadge :tone="r.type === 'review' ? 'info' : 'default'">{{ r.type_label }}</ApBadge></td>
                <td class="ap-mono" :title="r.run_uid">{{ r.run_uid.slice(0, 24) }}</td>
                <td>{{ r.as_of || '—' }}</td>
                <td class="ap-mono">{{ r.created }}</td>
                <td>
                  <button v-if="pickFile(r)" class="ck-link" type="button" @click="openArtifact(r)">预览</button>
                  <button v-if="pickFile(r)" class="ck-link" type="button" @click="downloadArtifact(r)">下载</button>
                  <span v-else class="ap-muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </ApCard>

    <!-- 产物预览弹窗 -->
    <div v-if="preview" class="ck-modal" @click.self="preview = null">
      <div class="ck-modal__panel">
        <div class="ck-modal__head ap-row">
          <strong class="ap-mono">{{ preview.file }}</strong>
          <button class="ck-link" type="button" @click="preview = null">关闭</button>
        </div>
        <p v-if="previewLoading" class="ap-muted">产物加载中…</p>
        <iframe
          v-else-if="preview.kind === 'html'"
          class="ck-frame"
          :srcdoc="preview.content"
          sandbox=""
          title="artifact 预览"
        ></iframe>
        <pre v-else class="ck-pre">{{ preview.content }}</pre>
      </div>
    </div>
  </main>
</template>

<style scoped>
.ck-filters { gap: 8px; margin-bottom: 12px; }
.ck-chip {
  border: 1px solid var(--ap-border, #d0d7de);
  background: transparent;
  border-radius: 999px;
  padding: 4px 14px;
  cursor: pointer;
  font-size: 13px;
}
.ck-chip--active { background: var(--ap-primary, #2563eb); color: #fff; border-color: transparent; }
.ck-table-scroll { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.ck-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ck-table th, .ck-table td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--ap-border, #e5e9ef); }
.ck-table th { color: var(--ap-muted, #6b7280); font-weight: 500; }
.ck-link { background: none; border: none; color: var(--ap-primary, #2563eb); cursor: pointer; padding: 0 6px 0 0; font-size: 13px; }
.ck-modal { position: fixed; inset: 0; background: rgb(0 0 0 / 45%); display: flex; align-items: center; justify-content: center; z-index: 60; }
.ck-modal__panel { background: var(--ap-bg, #fff); border-radius: 12px; width: min(920px, 92vw); max-height: 84vh; display: flex; flex-direction: column; padding: 14px 16px; }
.ck-modal__head { justify-content: space-between; margin-bottom: 10px; }
.ck-frame { width: 100%; height: 68vh; border: 0; }
.ck-pre { overflow: auto; max-height: 68vh; font-size: 12px; background: var(--ap-surface, #f6f8fa); padding: 12px; border-radius: 8px; }
</style>
