import { reactive } from 'vue';

import type { ApToastItem } from './types';

/** 全局单例提示队列（模块级），供 useToast 组合式与 ApToast 容器共享。 */
const state = reactive<{ items: ApToastItem[] }>({ items: [] });
let seq = 0;

const DEFAULT_DURATION = 3000;

/**
 * 轻量 Toast 组合式：show/success/error/info，返回 id 与移除方法。
 * 需在应用根挂载一次 <ApToast /> 容器。
 */
export function useToast() {
  function show(type: ApToastItem['type'], message: string, duration = DEFAULT_DURATION): number {
    const id = ++seq;
    state.items.push({ id, type, message });
    if (duration > 0) {
      setTimeout(() => remove(id), duration);
    }
    return id;
  }

  function remove(id: number) {
    const idx = state.items.findIndex((t) => t.id === id);
    if (idx >= 0) state.items.splice(idx, 1);
  }

  function clear() {
    state.items.splice(0, state.items.length);
  }

  return {
    items: state.items,
    show,
    remove,
    clear,
    success: (message: string, duration?: number) => show('success', message, duration),
    error: (message: string, duration?: number) => show('error', message, duration),
    info: (message: string, duration?: number) => show('info', message, duration),
  };
}
