<script setup lang="ts">
// AppLayout：按断点选择布局。≥1024px 用三栏工作台；<1024px（含平板）用移动端单列 + tabbar。
import { useBreakpoint } from '../composables/useBreakpoint';
import AppLayoutH5 from './AppLayoutH5.vue';
import AppLayoutPC from './AppLayoutPC.vue';

// width 可选：仅用于测试注入固定视口宽度；生产不传则跟随 window。
const props = defineProps<{ width?: number }>();
const { isPC } = useBreakpoint(props.width);
</script>

<template>
  <AppLayoutPC v-if="isPC" />
  <AppLayoutH5 v-else />
</template>
