<script setup lang="ts">
// AppLayoutH5：移动端单列纵向流 + 顶部品牌栏 + 底部 4 项 tabbar（含 iOS 安全区适配）。
import { useRoute } from 'vue-router';

const route = useRoute();

// 4 项 tabbar：组合 / 数据 / 运行 / 我的（§8；「我的」为占位入口）
const tabs = [
  { to: '/portfolios', label: '组合', match: '/portfolios', icon: 'M4 5h16v15H4z M8 3v4 M16 3v4 M4 10h16' },
  { to: '/data', label: '数据', match: '/data', icon: 'M3 19h18 M6 19V9 M11 19V5 M16 19v-7 M21 19v-4' },
  { to: '/runs', label: '运行', match: '/runs', icon: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7v5l3 2' },
  { to: '/me', label: '我的', match: '/me', icon: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4.5 21c0-3.8 3.4-5.6 7.5-5.6s7.5 1.8 7.5 5.6' },
];

const isActive = (match: string) =>
  route.path === match || route.path.startsWith(`${match}/`);
</script>

<template>
  <div class="ap-h5" data-test="h5-layout">
    <header class="ap-h5__head">
      <span class="ap-h5__brand">AlphaPocket</span>
      <span class="ap-kicker">V4-β</span>
    </header>

    <main class="ap-h5__main">
      <router-view />
    </main>

    <nav class="ap-h5__tabbar" data-test="h5-tabbar">
      <RouterLink
        v-for="tab in tabs"
        :key="tab.to"
        class="ap-h5__tab"
        :class="{ 'is-active': isActive(tab.match) }"
        :to="tab.to"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path :d="tab.icon" />
        </svg>
        <span>{{ tab.label }}</span>
      </RouterLink>
    </nav>
  </div>
</template>

<style scoped>
.ap-h5 {
  min-height: 100vh;
  background: var(--ap-paper);
}
.ap-h5__head {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  height: var(--ap-header-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--ap-sp-4);
  background: rgba(251, 250, 246, 0.94);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--ap-line);
}
.ap-h5__brand {
  font-family: var(--ap-font-serif);
  font-size: var(--ap-fs-md);
  font-weight: 600;
  letter-spacing: -0.01em;
}
.ap-h5__main {
  padding-top: var(--ap-header-h);
  /* 底部让位：tabbar + 安全区 + 全局免责声明 */
  padding-bottom: calc(
    var(--ap-tabbar-h) + var(--ap-safe-bottom) + var(--ap-disclaimer-h) + var(--ap-sp-4)
  );
}
.ap-h5__tabbar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: var(--ap-disclaimer-h);
  z-index: 100;
  display: flex;
  height: calc(var(--ap-tabbar-h) + var(--ap-safe-bottom));
  padding-bottom: var(--ap-safe-bottom);
  background: rgba(251, 250, 246, 0.96);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  border-top: 1px solid var(--ap-line);
}
.ap-h5__tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  color: #a79f8e;
  transition: 0.2s;
}
.ap-h5__tab svg {
  width: 20px;
  height: 20px;
  stroke: currentColor;
  fill: none;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.ap-h5__tab span {
  font-size: 10px;
  font-weight: 300;
}
.ap-h5__tab.is-active {
  color: var(--ap-seal);
}
.ap-h5__tab.is-active span {
  font-weight: 500;
}
</style>
