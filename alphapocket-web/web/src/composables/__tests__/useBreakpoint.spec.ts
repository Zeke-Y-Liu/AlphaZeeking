import { describe, expect, it } from 'vitest';

import { resolveBreakpoint, useBreakpoint } from '../useBreakpoint';

describe('resolveBreakpoint 断点边界', () => {
  it('767 → mobile，768 → tablet', () => {
    expect(resolveBreakpoint(767)).toBe('mobile');
    expect(resolveBreakpoint(768)).toBe('tablet');
  });

  it('1023 → tablet，1024 → pc', () => {
    expect(resolveBreakpoint(1023)).toBe('tablet');
    expect(resolveBreakpoint(1024)).toBe('pc');
  });

  it('极小/极大值落入首尾档', () => {
    expect(resolveBreakpoint(320)).toBe('mobile');
    expect(resolveBreakpoint(2560)).toBe('pc');
  });
});

describe('useBreakpoint 注入宽度', () => {
  it('375 为移动端', () => {
    const bp = useBreakpoint(375);
    expect(bp.width.value).toBe(375);
    expect(bp.isMobile.value).toBe(true);
    expect(bp.isTablet.value).toBe(false);
    expect(bp.isPC.value).toBe(false);
    expect(bp.current.value).toBe('mobile');
  });

  it('800 为平板', () => {
    const bp = useBreakpoint(800);
    expect(bp.isTablet.value).toBe(true);
    expect(bp.current.value).toBe('tablet');
  });

  it('1280 为 PC', () => {
    const bp = useBreakpoint(1280);
    expect(bp.isPC.value).toBe(true);
    expect(bp.isMobile.value).toBe(false);
  });
});
