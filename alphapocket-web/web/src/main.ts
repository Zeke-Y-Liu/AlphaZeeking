import { createPinia } from 'pinia';
import { createApp } from 'vue';

import 'virtual:uno.css';
import './styles/index.css';

import App from './App.vue';
import { configureAuth } from './api/client';
import { router } from './router';
import { useAuthStore } from './stores/auth';

const pinia = createPinia();
const app = createApp(App).use(pinia).use(router);

// 注入认证上下文：请求带 Bearer、401 单飞刷新、刷新失败清空并跳登录。
const auth = useAuthStore(pinia);
configureAuth({
  getAccess: () => auth.access,
  getRefresh: () => auth.refresh,
  refresh: () => auth.refreshTokens(),
  onRefreshFailed: () => auth.clear(),
  redirectToLogin: (redirect) => {
    if (router.currentRoute.value.path === '/login') return;
    void router.push({ path: '/login', query: { redirect } });
  },
});

app.mount('#app');
