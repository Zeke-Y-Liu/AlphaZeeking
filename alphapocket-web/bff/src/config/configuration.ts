/**
 * BFF 配置加载（T5.1）。
 * 从环境变量（含仓库根 ../.env，由 ConfigModule 注入 process.env）读取，全部带缺省值。
 * AP_JWT_* 本任务只读取并暴露，签发交给 T5.2。
 */
export const bffConfiguration = (): Record<string, unknown> => ({
  // core 上游地址（§3.2 端口表）
  coreUrl: process.env.CORE_URL ?? 'http://localhost:8082',
  // BFF 监听端口（缺省 8081）
  port: Number(process.env.PORT ?? 8081),
  // JWT 配置接缝（T5.2）：默认值与 core 的 AP_JWT_SECRET 缺省保持一致（无 .env 时可互通）
  jwt: {
    secret: process.env.AP_JWT_SECRET ?? 'dev-only-change-me',
    accessTtl: process.env.AP_JWT_ACCESS_TTL ?? '15m',
    refreshTtl: process.env.AP_JWT_REFRESH_TTL ?? '7d',
  },
  // 限流（秒 / 次数 / IP），缺省 60s / 100 次
  throttle: {
    ttlSeconds: Number(process.env.THROTTLE_TTL ?? 60),
    limit: Number(process.env.THROTTLE_LIMIT ?? 100),
  },
  // CORS 额外白名单（逗号分隔 origin，精确匹配）：用于 GitHub Pages 等跨源静态托管指向 BFF
  corsOrigins: (process.env.CORS_ORIGINS ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean),
});
