import { computed, getCurrentScope, onScopeDispose, ref } from 'vue';

/** 断点档位：mobile <768 ｜ tablet 768–1023 ｜ pc ≥1024（§2.1 / §8）。 */
export type BreakpointName = 'mobile' | 'tablet' | 'pc';

/** 纯函数：由视口宽度解析断点档位（供单测直接调用，无副作用）。 */
export function resolveBreakpoint(width: number): BreakpointName {
  if (width < 768) return 'mobile';
  if (width < 1024) return 'tablet';
  return 'pc';
}

/**
 * 响应式断点。
 * @param initialWidth 可选初始宽度：传入时锁定该值（便于 vitest/jsdom 注入）；
 *                     不传则读取 window.innerWidth 并监听 resize。
 */
export function useBreakpoint(initialWidth?: number) {
  const locked = initialWidth != null && Number.isFinite(initialWidth);
  const fallback =
    typeof window !== 'undefined' && Number.isFinite(window.innerWidth) ? window.innerWidth : 1024;

  const width = ref<number>(locked ? (initialWidth as number) : fallback);
  const current = computed<BreakpointName>(() => resolveBreakpoint(width.value));
  const isMobile = computed(() => current.value === 'mobile');
  const isTablet = computed(() => current.value === 'tablet');
  const isPC = computed(() => current.value === 'pc');

  if (!locked && typeof window !== 'undefined') {
    const onResize = () => {
      width.value = window.innerWidth;
    };
    width.value = window.innerWidth;
    window.addEventListener('resize', onResize);
    if (getCurrentScope()) {
      onScopeDispose(() => window.removeEventListener('resize', onResize));
    }
  }

  return { width, current, isMobile, isTablet, isPC };
}
