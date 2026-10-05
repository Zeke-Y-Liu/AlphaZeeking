import { INestApplication } from '@nestjs/common';

/**
 * CORS 配置（T5.2）。
 * 允许 dev 源（Vite 5173，localhost 与 127.0.0.1 同源别名）；
 * token 走 Authorization 头 / 响应体，不使用 cookie，故 credentials: false。
 * 生产由 web nginx 同源提供，此处仅服务本地 dev 跨端口场景。
 */
export function configureCors(app: INestApplication): void {
  app.enableCors({
    origin: ['http://localhost:5173', 'http://127.0.0.1:5173'],
    methods: ['GET', 'HEAD', 'PUT', 'PATCH', 'POST', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Authorization', 'Content-Type', 'X-Request-Id'],
    credentials: false,
  });
}
