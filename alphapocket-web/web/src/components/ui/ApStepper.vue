<script setup lang="ts">
// ApStepper：步骤条（steps + current），已完成/当前/未达三态，可点击回退已完成步骤。
import type { ApStepItem } from './types';

const props = defineProps<{ steps: ApStepItem[]; current: number }>();
const emit = defineEmits<{ change: [index: number] }>();

function stateOf(index: number) {
  if (index < props.current) return 'done';
  if (index === props.current) return 'active';
  return 'todo';
}

function select(index: number) {
  if (index <= props.current) emit('change', index);
}
</script>

<template>
  <ol class="ap-stepper">
    <li
      v-for="(step, index) in props.steps"
      :key="step.key"
      class="ap-stepper__step"
      :class="`is-${stateOf(index)}`"
    >
      <button class="ap-stepper__dot" type="button" @click="select(index)">
        {{ index + 1 }}
      </button>
      <div class="ap-stepper__txt">
        <b>{{ step.label }}</b>
        <span v-if="step.hint" class="ap-stepper__hint">{{ step.hint }}</span>
      </div>
      <i v-if="index < props.steps.length - 1" class="ap-stepper__line" />
    </li>
  </ol>
</template>

<style scoped>
.ap-stepper {
  display: flex;
  align-items: flex-start;
  gap: var(--ap-sp-2);
  margin: 0;
  padding: 0;
  list-style: none;
}
.ap-stepper__step {
  position: relative;
  flex: 1;
  display: flex;
  align-items: flex-start;
  gap: var(--ap-sp-2);
  min-width: 0;
}
.ap-stepper__dot {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  border-radius: var(--ap-radius-pill);
  border: 1.4px solid var(--ap-line);
  background: var(--ap-card);
  color: var(--ap-mute);
  font-family: var(--ap-font-mono);
  font-size: 11px;
  cursor: default;
  transition: 0.2s;
}
.ap-stepper__txt {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.ap-stepper__txt b {
  font-size: 12px;
  font-weight: 500;
  color: var(--ap-dim);
}
.ap-stepper__hint {
  font-family: var(--ap-font-mono);
  font-size: 9px;
  color: var(--ap-mute);
}
.ap-stepper__line {
  flex: 1;
  height: 1px;
  margin-top: 11px;
  background: var(--ap-line);
}
.ap-stepper__step.is-active .ap-stepper__dot {
  border-color: var(--ap-seal);
  background: var(--ap-seal);
  color: #fff;
}
.ap-stepper__step.is-active .ap-stepper__txt b {
  color: var(--ap-ink);
  font-weight: 600;
}
.ap-stepper__step.is-done .ap-stepper__dot {
  border-color: var(--ap-seal-line);
  color: var(--ap-seal);
  cursor: pointer;
}
.ap-stepper__step.is-done .ap-stepper__txt b {
  color: var(--ap-ink);
}
</style>
