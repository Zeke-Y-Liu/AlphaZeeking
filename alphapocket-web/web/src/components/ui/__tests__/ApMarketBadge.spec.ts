// ApMarketBadge（V4-β）：市场徽标 + 港股通「通」角标。
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import ApMarketBadge from '../ApMarketBadge.vue';

describe('ApMarketBadge', () => {
  it('A股：渲染灰档徽标，无港股通标记', () => {
    const wrapper = mount(ApMarketBadge, { props: { market: 'A' } });
    expect(wrapper.text()).toContain('A股');
    expect(wrapper.text()).not.toContain('通');
  });

  it('港股 + 港股通：渲染港股徽标与「通」角标', () => {
    const wrapper = mount(ApMarketBadge, { props: { market: 'HK', hkConnect: true } });
    expect(wrapper.text()).toContain('港股');
    expect(wrapper.text()).toContain('通');
  });

  it('港股非港股通：只有港股徽标，无「通」', () => {
    const wrapper = mount(ApMarketBadge, { props: { market: 'HK', hkConnect: false } });
    expect(wrapper.text()).toContain('港股');
    expect(wrapper.text()).not.toContain('通');
  });

  it('美股：渲染美股徽标；误传 hkConnect 也不显示「通」（仅港股生效）', () => {
    const wrapper = mount(ApMarketBadge, { props: { market: 'US', hkConnect: true } });
    expect(wrapper.text()).toContain('美股');
    expect(wrapper.text()).not.toContain('通');
  });

  it('未知市场：原样展示代码', () => {
    const wrapper = mount(ApMarketBadge, { props: { market: 'XX' } });
    expect(wrapper.text()).toContain('XX');
  });
});
