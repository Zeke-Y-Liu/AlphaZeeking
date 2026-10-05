import { mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import type { Router } from 'vue-router';
import { describe, expect, it } from 'vitest';

import AppLayout from '../AppLayout.vue';
import AppLayoutH5 from '../AppLayoutH5.vue';
import AppLayoutPC from '../AppLayoutPC.vue';

const Stub = { template: '<div class="stub-page" />' };

async function makeRouter(): Promise<Router> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/portfolios' },
      { path: '/portfolios', name: 'portfolios', component: Stub },
      { path: '/portfolios/new', name: 'portfolio-new', component: Stub },
      { path: '/data', name: 'data', component: Stub },
      { path: '/runs', name: 'runs', component: Stub },
      { path: '/me', name: 'me', component: Stub },
    ],
  });
  await router.push('/portfolios');
  await router.isReady();
  return router;
}

describe('AppLayout 断点切换', () => {
  it('isMobile（375）渲染 H5 单列 + 4 项 tabbar', async () => {
    const router = await makeRouter();
    const wrapper = mount(AppLayout, {
      props: { width: 375 },
      global: { plugins: [router] },
    });

    expect(wrapper.find('[data-test="h5-layout"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pc-layout"]').exists()).toBe(false);

    const tabbar = wrapper.find('[data-test="h5-tabbar"]');
    expect(tabbar.exists()).toBe(true);
    const tabs = tabbar.findAll('a');
    expect(tabs).toHaveLength(4);
    expect(tabs.map((t) => t.text())).toEqual(['组合', '数据', '运行', '我的']);
  });

  it('isPC（1280）渲染三栏工作台', async () => {
    const router = await makeRouter();
    const wrapper = mount(AppLayout, {
      props: { width: 1280 },
      global: { plugins: [router] },
    });

    expect(wrapper.find('[data-test="pc-layout"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="h5-layout"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pc-nav"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pc-main"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pc-context"]').exists()).toBe(true);
  });

  it('平板（800）回落 H5 布局', async () => {
    const router = await makeRouter();
    const wrapper = mount(AppLayout, {
      props: { width: 800 },
      global: { plugins: [router] },
    });
    expect(wrapper.find('[data-test="h5-layout"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pc-layout"]').exists()).toBe(false);
  });
});

describe('AppLayoutH5 tabbar 高亮', () => {
  it('当前路由对应 tab 高亮', async () => {
    const router = await makeRouter();
    const wrapper = mount(AppLayoutH5, { global: { plugins: [router] } });
    const active = wrapper.find('[data-test="h5-tabbar"] a.is-active');
    expect(active.exists()).toBe(true);
    expect(active.text()).toBe('组合');
  });
});

describe('AppLayoutPC 三栏结构', () => {
  it('含导航树、主内容、上下文栏', async () => {
    const router = await makeRouter();
    const wrapper = mount(AppLayoutPC, { global: { plugins: [router] } });
    expect(wrapper.find('[data-test="pc-nav"]').text()).toContain('组合列表');
    expect(wrapper.find('[data-test="pc-main"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pc-context"]').text()).toContain('上下文栏占位');
  });
});
