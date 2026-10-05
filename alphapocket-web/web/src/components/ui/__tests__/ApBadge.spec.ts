import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import ApBadge from '../ApBadge.vue';

describe('ApBadge', () => {
  it('默认 tone 渲染 default 类名', () => {
    const wrapper = mount(ApBadge, { slots: { default: '标签' } });
    expect(wrapper.classes()).toContain('ap-badge--default');
    expect(wrapper.text()).toBe('标签');
  });

  it('tone=success 渲染对应类名', () => {
    const wrapper = mount(ApBadge, { props: { tone: 'success' }, slots: { default: '通过' } });
    expect(wrapper.classes()).toContain('ap-badge--success');
  });

  it('tone=danger 渲染对应类名', () => {
    const wrapper = mount(ApBadge, { props: { tone: 'danger' } });
    expect(wrapper.classes()).toContain('ap-badge--danger');
  });
});
