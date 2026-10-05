<script setup lang="ts">
// ApModal：弹窗（teleport 到 body + 遮罩），点击遮罩/关闭按钮/Esc 触发关闭事件。
import { onBeforeUnmount, watch } from 'vue';

const props = withDefaults(defineProps<{ modelValue: boolean; title?: string; width?: string }>(), {
  title: '',
  width: '480px',
});
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; close: [] }>();

function close() {
  emit('update:modelValue', false);
  emit('close');
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close();
}

function bindKey(open: boolean) {
  if (typeof document === 'undefined') return;
  if (open) document.addEventListener('keydown', onKeydown);
  else document.removeEventListener('keydown', onKeydown);
}

watch(() => props.modelValue, bindKey, { immediate: true });
onBeforeUnmount(() => bindKey(false));
</script>

<template>
  <Teleport to="body">
    <div v-if="props.modelValue" class="ap-modal" @click.self="close">
      <div class="ap-modal__panel" :style="{ width: props.width }" role="dialog" aria-modal="true">
        <header class="ap-modal__head">
          <h3 class="ap-modal__title">{{ props.title }}</h3>
          <button class="ap-modal__close" type="button" aria-label="关闭" @click="close">×</button>
        </header>
        <div class="ap-modal__body">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="ap-modal__foot">
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.ap-modal {
  position: fixed;
  inset: 0;
  z-index: 250;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--ap-sp-4);
  background: rgba(28, 27, 24, 0.45);
  backdrop-filter: blur(2px);
}
.ap-modal__panel {
  max-width: 100%;
  background: var(--ap-card);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius-card);
  box-shadow: var(--ap-shadow-lg);
  overflow: hidden;
}
.ap-modal__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 15px;
  border-bottom: 1px solid var(--ap-line);
}
.ap-modal__title {
  font-size: var(--ap-fs-md);
  font-weight: 600;
}
.ap-modal__close {
  appearance: none;
  border: 0;
  background: transparent;
  font-size: 20px;
  line-height: 1;
  color: var(--ap-mute);
  cursor: pointer;
}
.ap-modal__close:hover {
  color: var(--ap-seal);
}
.ap-modal__body {
  padding: 15px;
}
.ap-modal__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--ap-sp-2);
  padding: 12px 15px;
  border-top: 1px solid var(--ap-line);
  background: var(--ap-card2);
}
</style>
