<script setup lang="ts">
// 应用根组件：接入响应式布局 + 全局 Toast 容器 + 固定免责声明（合规要求）。
// meta.layout === 'blank' 的页面（如登录页）免全局布局：不渲染 H5 tabbar / PC 三栏。
import { computed } from 'vue';
import { useRoute } from 'vue-router';

import { ApToast } from './components/ui';
import AppLayout from './layouts/AppLayout.vue';

const route = useRoute();
const isBlank = computed(() => route.meta.layout === 'blank');
</script>

<template>
  <AppLayout v-if="!isBlank" />
  <router-view v-else />
  <ApToast />
  <footer class="ap-disclaimer">数据来自公开来源，仅供研究参考，不构成投资建议</footer>
</template>
