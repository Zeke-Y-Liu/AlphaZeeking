<script setup lang="ts">
// 「我的」（H5 tabbar 第 4 项）：快捷入口 + 账户/偏好占位（后续任务实现）。
// 快捷入口：H5 4-tab 设计约束下，/today、/companies 等无 tabbar 入口的页面从此进入
//（PC 端已有侧栏「总览 → 今日结论」「公司 → 单公司分析」，此处为 H5 兜底入口）。
import { ApCard } from '../components/ui';

const quickLinks = [
  { to: '/today', label: '今日结论', desc: '各组合当日操作建议' },
  { to: '/companies', label: '单公司分析', desc: '基本面 / 行业 / 超卖 / 消息' },
  { to: '/engines', label: '引擎与因子', desc: '指标注册表' },
];
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head">
      <h1 class="ap-title">我的</h1>
      <p class="ap-page__desc">账户与偏好（占位，后续任务实现）。</p>
    </div>

    <ApCard title="快捷入口">
      <nav class="me-links">
        <RouterLink v-for="l in quickLinks" :key="l.to" class="me-link" :to="l.to">
          <span class="me-link__label">{{ l.label }}</span>
          <span class="ap-muted">{{ l.desc }}</span>
        </RouterLink>
      </nav>
    </ApCard>

    <ApCard>
      <div class="ap-todo">待实现：账户信息、登出、2FA 扩展位、偏好设置。</div>
    </ApCard>
  </main>
</template>

<style scoped>
.me-links { display: flex; flex-direction: column; gap: var(--ap-sp-2); }
.me-link {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--ap-sp-2);
  padding: var(--ap-sp-2) var(--ap-sp-3);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  text-decoration: none;
  color: var(--ap-ink);
}
.me-link__label { font-weight: 600; }
</style>
