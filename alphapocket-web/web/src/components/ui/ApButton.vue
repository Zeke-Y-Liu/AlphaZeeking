<script setup lang="ts">
// ApButton：基础按钮，风格 primary/ghost/danger，支持尺寸、loading、disabled。
const props = withDefaults(
  defineProps<{
    variant?: 'primary' | 'ghost' | 'danger';
    size?: 'sm' | 'md' | 'lg';
    loading?: boolean;
    disabled?: boolean;
    type?: 'button' | 'submit' | 'reset';
  }>(),
  { variant: 'primary', size: 'md', loading: false, disabled: false, type: 'button' },
);

const emit = defineEmits<{ click: [event: MouseEvent] }>();

const isDisabled = () => props.disabled || props.loading;

function onClick(event: MouseEvent) {
  if (isDisabled()) return;
  emit('click', event);
}
</script>

<template>
  <button
    class="ap-btn"
    :class="[`ap-btn--${props.variant}`, `ap-btn--${props.size}`, { 'is-loading': props.loading }]"
    :type="props.type"
    :disabled="isDisabled()"
    @click="onClick"
  >
    <span v-if="props.loading" class="ap-btn__spinner" aria-hidden="true" />
    <slot />
  </button>
</template>

<style scoped>
.ap-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 1px solid transparent;
  border-radius: var(--ap-radius);
  font-family: var(--ap-font-sans);
  font-weight: 500;
  cursor: pointer;
  transition: 0.2s;
  white-space: nowrap;
}
.ap-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.ap-btn--sm {
  height: 26px;
  padding: 0 10px;
  font-size: 12px;
}
.ap-btn--md {
  height: 32px;
  padding: 0 14px;
  font-size: 13px;
}
.ap-btn--lg {
  height: 40px;
  padding: 0 20px;
  font-size: 14px;
}
/* 主按钮：印章红 */
.ap-btn--primary {
  background: var(--ap-seal);
  color: #fff;
  border-color: var(--ap-seal);
}
.ap-btn--primary:hover:not(:disabled) {
  background: #96301f;
}
/* 幽灵按钮 */
.ap-btn--ghost {
  background: var(--ap-card);
  color: var(--ap-dim);
  border-color: var(--ap-line);
}
.ap-btn--ghost:hover:not(:disabled) {
  border-color: var(--ap-seal);
  color: var(--ap-seal);
}
/* 危险按钮 */
.ap-btn--danger {
  background: transparent;
  color: var(--ap-danger);
  border-color: var(--ap-seal-line);
}
.ap-btn--danger:hover:not(:disabled) {
  background: var(--ap-seal-soft);
}
.ap-btn__spinner {
  width: 12px;
  height: 12px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: ap-btn-spin 0.7s linear infinite;
}
@keyframes ap-btn-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
