<script setup lang="ts">
// AppLayoutPC：三栏工作台 —— 左侧导航树（组合/数据/运行）+ 中间主内容 + 右侧上下文栏（占位）。
import { useRoute } from 'vue-router';

const route = useRoute();

const navGroups = [
  {
    key: '组合',
    items: [
      { to: '/portfolios', label: '组合列表' },
      { to: '/portfolios/new', label: '新建组合' },
    ],
  },
  {
    key: '公司',
    items: [{ to: '/companies', label: '单公司分析' }],
  },
  {
    key: '引擎',
    items: [{ to: '/engines', label: '引擎与因子' }],
  },
  {
    key: '数据',
    items: [{ to: '/data', label: '数据看板' }],
  },
  {
    key: '运行',
    items: [{ to: '/runs', label: '运行历史' }],
  },
];

function isActive(to: string): boolean {
  if (route.path === to) return true;
  // 组合详情（/portfolios/:id）回落到「组合列表」，但排除「新建组合」
  return to === '/portfolios' && route.path.startsWith('/portfolios/') && route.path !== '/portfolios/new';
}
</script>

<template>
  <div class="ap-pc" data-test="pc-layout">
    <aside class="ap-pc__nav" data-test="pc-nav">
      <div class="ap-pc__brand">
        <span>AlphaPocket</span>
        <em>V4-β</em>
      </div>
      <nav class="ap-pc__tree">
        <div v-for="group in navGroups" :key="group.key" class="ap-pc__group">
          <div class="ap-kicker ap-pc__grouplbl">{{ group.key }}</div>
          <RouterLink
            v-for="item in group.items"
            :key="item.to"
            class="ap-pc__link"
            :class="{ 'is-active': isActive(item.to) }"
            :to="item.to"
          >
            {{ item.label }}
          </RouterLink>
        </div>
      </nav>
    </aside>

    <main class="ap-pc__main" data-test="pc-main">
      <router-view />
    </main>

    <aside class="ap-pc__context" data-test="pc-context">
      <div class="ap-kicker ap-pc__ctxtitle">Context</div>
      <div class="ap-todo">
        <p>上下文栏占位：后续承载 lineage（数据血缘）、待办、Agent 会话。</p>
        <p class="ap-pc__ctxtag">T6.x 之后接入</p>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.ap-pc {
  display: grid;
  grid-template-columns: var(--ap-nav-w) minmax(0, 1fr) var(--ap-context-w);
  height: calc(100vh - var(--ap-disclaimer-h));
  background: var(--ap-paper);
}
.ap-pc__nav,
.ap-pc__context {
  overflow-y: auto;
  background: var(--ap-card2);
  border-right: 1px solid var(--ap-line);
}
.ap-pc__context {
  border-right: 0;
  border-left: 1px solid var(--ap-line);
  padding: var(--ap-sp-4);
}
.ap-pc__brand {
  display: flex;
  align-items: baseline;
  gap: var(--ap-sp-2);
  padding: var(--ap-sp-4);
  border-bottom: 1px solid var(--ap-line);
}
.ap-pc__brand span {
  font-family: var(--ap-font-serif);
  font-size: var(--ap-fs-md);
  font-weight: 600;
}
.ap-pc__brand em {
  font-family: var(--ap-font-mono);
  font-style: normal;
  font-size: 10px;
  color: var(--ap-seal);
}
.ap-pc__tree {
  padding: var(--ap-sp-3) var(--ap-sp-2);
}
.ap-pc__group {
  margin-bottom: var(--ap-sp-4);
}
.ap-pc__grouplbl {
  padding: 0 var(--ap-sp-2);
  margin-bottom: var(--ap-sp-2);
}
.ap-pc__link {
  display: block;
  padding: 7px 10px;
  border-radius: var(--ap-radius);
  font-size: var(--ap-fs-base);
  color: var(--ap-dim);
  transition: 0.2s;
}
.ap-pc__link:hover {
  background: rgba(28, 27, 24, 0.045);
  color: var(--ap-ink);
}
.ap-pc__link.is-active {
  background: var(--ap-seal-soft);
  color: var(--ap-seal);
  font-weight: 500;
}
.ap-pc__main {
  overflow-y: auto;
  background: var(--ap-paper);
}
.ap-pc__ctxtitle {
  margin-bottom: var(--ap-sp-3);
}
.ap-pc__ctxtag {
  margin-top: var(--ap-sp-2);
  font-family: var(--ap-font-mono);
  font-size: 9px;
  color: var(--ap-mute);
}
</style>
