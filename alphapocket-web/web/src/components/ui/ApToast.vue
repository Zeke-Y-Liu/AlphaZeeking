<script setup lang="ts">
// ApToast：提示容器（固定右上/移动端顶部），渲染 useToast 队列，支持 success/error/info。
import { useToast } from './useToast';

const { items, remove } = useToast();
</script>

<template>
  <div class="ap-toast" role="status" aria-live="polite">
    <TransitionGroup name="ap-toast">
      <div
        v-for="item in items"
        :key="item.id"
        class="ap-toast__item"
        :class="`ap-toast__item--${item.type}`"
        @click="remove(item.id)"
      >
        {{ item.message }}
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.ap-toast {
  position: fixed;
  top: var(--ap-sp-4);
  right: var(--ap-sp-4);
  z-index: 300;
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-2);
  pointer-events: none;
}
.ap-toast__item {
  pointer-events: auto;
  cursor: pointer;
  min-width: 160px;
  max-width: 320px;
  padding: 9px 13px;
  font-size: 12px;
  border-radius: var(--ap-radius);
  border: 1px solid var(--ap-line);
  background: var(--ap-card);
  color: var(--ap-ink);
  box-shadow: var(--ap-shadow);
}
.ap-toast__item--success {
  border-color: rgba(30, 122, 94, 0.45);
  color: var(--ap-success);
}
.ap-toast__item--error {
  border-color: var(--ap-seal-line);
  color: var(--ap-danger);
}
.ap-toast__item--info {
  border-color: rgba(44, 95, 138, 0.4);
  color: var(--ap-info);
}
.ap-toast-enter-active,
.ap-toast-leave-active {
  transition: all 0.25s ease;
}
.ap-toast-enter-from,
.ap-toast-leave-to {
  opacity: 0;
  transform: translateX(12px);
}
@media (max-width: 767px) {
  .ap-toast {
    left: var(--ap-sp-4);
    right: var(--ap-sp-4);
    top: calc(var(--ap-sp-4) + var(--ap-header-h));
  }
  .ap-toast__item {
    max-width: none;
  }
}
</style>
