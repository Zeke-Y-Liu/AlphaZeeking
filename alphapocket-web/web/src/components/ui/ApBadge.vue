<script setup lang="ts">
// ApBadge：状态/类型/证据徽章（对应原型 .ev / .stamp 视觉），tone 决定语义配色。
import type { BadgeTone } from './types';

const props = withDefaults(defineProps<{ tone?: BadgeTone }>(), { tone: 'default' });
</script>

<template>
  <span class="ap-badge" :class="`ap-badge--${props.tone}`">
    <slot />
  </span>
</template>

<style scoped>
.ap-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 7px;
  border: 1px solid;
  border-radius: var(--ap-radius-xs);
  font-family: var(--ap-font-mono);
  font-size: 10px;
  line-height: 1.4;
  letter-spacing: 0.04em;
  white-space: nowrap;
}
.ap-badge--default {
  color: var(--ap-dim);
  border-color: #bcb4a3;
  background: var(--ap-card2);
}
.ap-badge--success {
  color: var(--ap-success);
  border-color: rgba(30, 122, 94, 0.45);
  background: rgba(30, 122, 94, 0.07);
}
.ap-badge--warn {
  color: var(--ap-warn);
  border-color: rgba(176, 140, 60, 0.5);
  background: rgba(176, 140, 60, 0.09);
}
.ap-badge--danger {
  color: var(--ap-danger);
  border-color: var(--ap-seal-line);
  background: var(--ap-seal-soft);
}
.ap-badge--info {
  color: var(--ap-info);
  border-color: rgba(44, 95, 138, 0.4);
  background: rgba(44, 95, 138, 0.07);
}
</style>
