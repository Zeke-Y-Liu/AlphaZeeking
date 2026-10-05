/**
 * 极简路径匹配（T5.3）。
 * 用于中间件/拦截器在通配代理之外，精确识别需要特殊处理的少量端点。
 * 语法：`*` 匹配任意剩余字符，`:name` 匹配单个路径段；其余字符按字面量处理。
 */
function escapeSegment(segment: string): string {
  return segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 编译路径表达式为锚定正则。 */
export function compilePath(pattern: string): RegExp {
  const body = pattern
    .split('/')
    .map((segment) => {
      if (segment === '*') return '.*';
      if (segment.startsWith(':')) return '[^/]+';
      return escapeSegment(segment);
    })
    .join('/');
  return new RegExp(`^${body}$`);
}

/** 判断请求路径（不含 query）是否命中表达式。 */
export function matchPath(pathname: string, pattern: string): boolean {
  return compilePath(pattern).test(pathname);
}

/** 判断请求是否命中「方法 + 路径表达式」中的任一路由。 */
export function matchRoute(
  method: string,
  pathname: string,
  routes: ReadonlyArray<{ method: string; pattern: string }>,
): boolean {
  return routes.some((route) => route.method === method && matchPath(pathname, route.pattern));
}
