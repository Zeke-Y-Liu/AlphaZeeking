<script setup lang="ts">
// 运行历史占位（T6.7：type/status 筛选 + 产物直达；PC 表格 / H5 列表）。
import { ApButton, ApCard, ApTable } from '../components/ui';
import type { ApTableColumn } from '../components/ui';

const columns: ApTableColumn[] = [
  { key: 'run_uid', label: 'Run' },
  { key: 'type', label: '类型' },
  { key: 'as_of', label: '日期' },
  { key: 'artifact', label: '产物' },
];
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row">
      <div>
        <h1 class="ap-title">运行历史</h1>
        <p class="ap-page__desc">运行历史（PC 表格 / H5 列表）。占位（T6.7）。</p>
      </div>
      <ApButton variant="ghost" disabled>筛选</ApButton>
    </div>

    <ApCard>
      <ApTable :columns="columns" :rows="[]" empty-text="待实现：运行历史列表（/v1/runs）" />
    </ApCard>
  </main>
</template>
