import { AsyncLocalStorage } from 'node:async_hooks';

/** 请求级追踪上下文（T5.1）。 */
export interface TraceContext {
  traceId: string;
}

/**
 * 以 AsyncLocalStorage 承载当前请求的 traceId。
 * 中间件在请求进入时 run()，CoreClientService 在任意深度读取并透传到 core。
 */
export const traceStore = new AsyncLocalStorage<TraceContext>();
