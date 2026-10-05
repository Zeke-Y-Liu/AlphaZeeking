<script setup lang="ts">
// ApTabs：受控标签页（v-model），点击切换 active，内容由默认插槽按 active 渲染。
import type { ApTabItem } from './types';

const props = defineProps<{ tabs: ApTabItem[]; modelValue: string }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();

function select(tab: ApTabItem) {
  if (tab.disabled || tab.key === props.modelValue) return;
  emit('update:modelValue', tab.key);
}
</script>

<template>
  <div class="ap-tabs">
    <div class="ap-tabs__nav" role="tablist">
      <button
        v-for="tab in props.tabs"
        :key="tab.key"
        class="ap-tabs__tab"
        :class="{ 'is-active': tab.key === props.modelValue, 'is-disabled': tab.disabled }"
        role="tab"
        :aria-selected="tab.key === props.modelValue"
        :disabled="tab.disabled"
        @click="select(tab)"
      >
        {{ tab.label }}
      </button>
    </div>
    <div class="ap-tabs__panel">
      <slot :active="props.modelValue" />
    </div>
  </div>
</template>

<style scoped>
.ap-tabs__nav {
  display: flex;
  gap: var(--ap-sp-1);
  border-bottom: 1px solid var(--ap-line);
}
.ap-tabs__tab {
  appearance: none;
  border: 0;
  background: transparent;
  padding: 9px 14px;
  font-size: var(--ap-fs-base);
  color: var(--ap-dim);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: 0.2s;
}
.ap-tabs__tab:hover:not(:disabled) {
  color: var(--ap-ink);
}
.ap-tabs__tab.is-active {
  color: var(--ap-seal);
  font-weight: 600;
  border-bottom-color: var(--ap-seal);
}
.ap-tabs__tab.is-disabled {
  color: var(--ap-mute);
  cursor: not-allowed;
}
.ap-tabs__panel {
  padding-top: var(--ap-sp-4);
}
</style>
