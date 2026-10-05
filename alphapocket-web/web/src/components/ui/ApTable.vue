<script setup lang="ts">
// ApTable：数据表格（列定义 props + 行数据），支持紧凑/舒适密度与具名单元插槽。
import type { ApTableColumn } from './types';

const props = withDefaults(
  defineProps<{
    columns: ApTableColumn[];
    rows: Record<string, unknown>[];
    density?: 'compact' | 'comfortable';
    rowKey?: string;
    emptyText?: string;
  }>(),
  { density: 'comfortable', rowKey: '', emptyText: '暂无数据' },
);

const alignClass = (align?: ApTableColumn['align']) =>
  align ? `is-${align}` : 'is-left';

const display = (value: unknown) =>
  value === null || value === undefined || value === '' ? '—' : String(value);
</script>

<template>
  <div class="ap-table__wrap">
    <table class="ap-table" :class="`ap-table--${props.density}`">
      <thead>
        <tr>
          <th
            v-for="col in props.columns"
            :key="col.key"
            :style="col.width ? { width: col.width } : undefined"
            :class="alignClass(col.align)"
          >
            {{ col.label }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in props.rows" :key="props.rowKey ? String(row[props.rowKey]) : index">
          <td v-for="col in props.columns" :key="col.key" :class="alignClass(col.align)">
            <slot :name="`cell:${col.key}`" :row="row" :value="row[col.key]" :index="index">
              {{ display(row[col.key]) }}
            </slot>
          </td>
        </tr>
        <tr v-if="props.rows.length === 0">
          <td class="ap-table__empty" :colspan="props.columns.length">
            <slot name="empty">{{ props.emptyText }}</slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.ap-table__wrap {
  width: 100%;
  overflow-x: auto;
}
.ap-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--ap-fs-sm);
}
.ap-table th {
  height: var(--ap-table-head-h);
  padding: 0 10px;
  text-align: left;
  font-family: var(--ap-font-mono);
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.08em;
  color: var(--ap-mute);
  background: var(--ap-card2);
  border-bottom: 1px solid var(--ap-line);
  white-space: nowrap;
}
.ap-table td {
  padding: 0 10px;
  border-bottom: 1px solid var(--ap-hair);
  color: var(--ap-ink);
}
.ap-table--compact td {
  height: var(--ap-row-compact);
}
.ap-table--comfortable td {
  height: var(--ap-row-comfortable);
}
.ap-table tbody tr:last-child td {
  border-bottom: 0;
}
.ap-table .is-right {
  text-align: right;
}
.ap-table .is-center {
  text-align: center;
}
.ap-table .is-left {
  text-align: left;
}
.ap-table__empty {
  text-align: center;
  color: var(--ap-mute);
  font-weight: 300;
  height: 64px;
}
</style>
