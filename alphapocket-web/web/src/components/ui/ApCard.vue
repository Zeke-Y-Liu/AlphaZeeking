<script setup lang="ts">
// ApCard：纸质卡片容器（对应原型 .card），支持标题与额外操作插槽。
const props = withDefaults(defineProps<{ title?: string; flat?: boolean }>(), {
  title: '',
  flat: false,
});
</script>

<template>
  <section class="ap-card" :class="{ 'ap-card--flat': props.flat }">
    <header v-if="props.title || $slots.header || $slots.extra" class="ap-card__head">
      <slot name="header">
        <h3 class="ap-card__title">{{ props.title }}</h3>
      </slot>
      <div v-if="$slots.extra" class="ap-card__extra">
        <slot name="extra" />
      </div>
    </header>
    <div class="ap-card__body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.ap-card {
  background: var(--ap-card);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius-card);
  padding: 14px 15px;
}
.ap-card--flat {
  background: var(--ap-card2);
}
.ap-card__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--ap-sp-2);
  margin-bottom: var(--ap-sp-3);
}
.ap-card__title {
  font-size: var(--ap-fs-md);
  font-weight: 600;
}
.ap-card__extra {
  font-family: var(--ap-font-mono);
  font-size: 10px;
  color: var(--ap-mute);
}
</style>
