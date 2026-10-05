import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import ApButton from '../ApButton.vue';

describe('ApButton', () => {
  it('正常点击触发 click 事件', async () => {
    const wrapper = mount(ApButton, { slots: { default: '运行' } });
    await wrapper.trigger('click');
    expect(wrapper.emitted('click')).toHaveLength(1);
  });

  it('disabled 不触发 click', async () => {
    const wrapper = mount(ApButton, { props: { disabled: true }, slots: { default: '运行' } });
    expect(wrapper.attributes('disabled')).toBeDefined();
    await wrapper.trigger('click');
    expect(wrapper.emitted('click')).toBeUndefined();
  });

  it('loading 时禁用且不触发 click', async () => {
    const wrapper = mount(ApButton, { props: { loading: true }, slots: { default: '提交' } });
    expect(wrapper.classes()).toContain('is-loading');
    await wrapper.trigger('click');
    expect(wrapper.emitted('click')).toBeUndefined();
  });

  it('variant / size 映射为类名', () => {
    const wrapper = mount(ApButton, { props: { variant: 'danger', size: 'sm' } });
    expect(wrapper.classes()).toContain('ap-btn--danger');
    expect(wrapper.classes()).toContain('ap-btn--sm');
  });
});
