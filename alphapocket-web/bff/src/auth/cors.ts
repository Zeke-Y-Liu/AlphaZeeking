import { INestApplication } from '@nestjs/common';

/**
 * CORS 配置（T5.2）。
 * 允许 dev 源（Vite 5173，localhost 与 127.0.0.1 同源别名）；
 * token 走 Authorization 头 / 响应体，不使用 cookie，故 credentials: false。
 * 生产由 web nginx 同源提供，此处仅服务本地 dev 跨端口场景。
 * 内网部署（T-内网）：追加本机 IP 源与网段内任意 5173 源，供局域网用户直访。
 */
export function configureCors(app: INestApplication): void {
  app.enableCors({
    origin: (origin, cb) => {
      // 非浏览器请求（curl/服务间调用）无 Origin 头，直接放行
      if (!origin) return cb(null, true);
      if (/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)) return cb(null, true);
      // 内网 IPv4 源（10.x.x.x / 172.16-31.x.x / 192.168.x.x）任意端口放行。
      // 10 开头占 1 段需再跟 3 段；172.16-31 / 192.168 占 2 段需再跟 2 段。
      if (
        /^https?:\/\/(10|172\.(1[6-9]|2\d|3[01])|192\.168)(\.\d{1,3}){2,3}(:\d+)?$/.test(origin)
      ) {
        return cb(null, true);
      }
      return cb(null, false);
    },
    methods: ['GET', 'HEAD', 'PUT', 'PATCH', 'POST', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Authorization', 'Content-Type', 'X-Request-Id'],
    credentials: false,
  });
}
