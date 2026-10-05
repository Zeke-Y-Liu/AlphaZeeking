import { createRouter, createWebHistory } from 'vue-router';
import type { NavigationGuard, RouteRecordRaw, RouterHistory } from 'vue-router';

import { useAuthStore } from '../stores/auth';

// 路由表（§8 前端页面规格）。业务页当前均为占位视图（含标题 + 待实现提示），
// 数据/交互分别在 T6.2–T6.7 实现。注意 /portfolios/new 必须先于 /portfolios/:id 注册。
// meta.requiresAuth 默认 true；仅 /login 显式 false。layout: 'blank' 的页面免全局布局。
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/portfolios' },
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { requiresAuth: false, layout: 'blank', title: '登录' },
  },
  {
    path: '/portfolios',
    name: 'portfolios',
    component: () => import('../views/PortfoliosView.vue'),
    meta: { title: '组合' },
  },
  {
    path: '/portfolios/new',
    name: 'portfolio-new',
    component: () => import('../views/NewPortfolioView.vue'),
    meta: { title: '新建组合' },
  },
  {
    path: '/portfolios/:id',
    name: 'portfolio-detail',
    component: () => import('../views/PortfolioDetailView.vue'),
    meta: { title: '组合详情' },
  },
  {
    path: '/portfolios/:id/report',
    name: 'portfolio-report',
    component: () => import('../views/PortfolioReportView.vue'),
    meta: { title: '组合报告' },
  },
  {
    path: '/portfolios/:id/backtest',
    name: 'portfolio-backtest',
    component: () => import('../views/BacktestView.vue'),
    meta: { title: '回测' },
  },
  {
    path: '/portfolios/:id/rebalance',
    name: 'portfolio-rebalance',
    component: () => import('../views/RebalanceView.vue'),
    meta: { title: '调仓' },
  },
  {
    path: '/data',
    name: 'data',
    component: () => import('../views/DataView.vue'),
    meta: { title: '数据' },
  },
  {
    path: '/runs',
    name: 'runs',
    component: () => import('../views/RunsView.vue'),
    meta: { title: '运行历史' },
  },
  {
    path: '/me',
    name: 'me',
    component: () => import('../views/MeView.vue'),
    meta: { title: '我的' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('../views/NotFoundView.vue'),
    meta: { title: '404' },
  },
];

const APP_TITLE = 'AlphaPocket V4-β';

/**
 * 认证守卫：
 * - 首次导航前执行 auth.restore()（从 localStorage 恢复凭证）；
 * - 受保护路由（meta.requiresAuth !== false）未认证 → /login?redirect=<原路径>；
 * - 已认证访问 /login → redirect 参数目标（安全校验后）或 /portfolios。
 */
export const authGuard: NavigationGuard = (to) => {
  const auth = useAuthStore();
  if (!auth.hydrated) auth.restore();
  const requiresAuth = to.meta.requiresAuth !== false;
  if (requiresAuth && !auth.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  if (to.path === '/login' && auth.isAuthenticated) {
    const redirect = to.query.redirect;
    if (typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//')) {
      return redirect;
    }
    return '/portfolios';
  }
  return true;
};

/** 工厂：便于测试注入 memory history；生产使用默认 web history。 */
export function createAppRouter(history: RouterHistory = createWebHistory()) {
  const router = createRouter({ history, routes });
  router.beforeEach(authGuard);
  // 页面标题（meta.title）→ document.title
  router.afterEach((to) => {
    if (typeof document === 'undefined') return;
    const title = to.meta.title as string | undefined;
    document.title = title ? `${title} · ${APP_TITLE}` : APP_TITLE;
  });
  return router;
}

export const router = createAppRouter();

export default router;
