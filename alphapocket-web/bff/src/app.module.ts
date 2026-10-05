import { MiddlewareConsumer, Module, NestModule } from '@nestjs/common';
import { APP_FILTER, APP_GUARD, APP_INTERCEPTOR } from '@nestjs/core';
import { ThrottlerGuard, ThrottlerModule } from '@nestjs/throttler';

import { AppConfigModule } from './config/config.module';
import { AppConfigService } from './config/app-config.service';
import { AuthController } from './auth/auth.controller';
import { AuthModule } from './auth/auth.module';
import { CacheControlMiddleware } from './cache/cache-control.middleware';
import { DtoValidationInterceptor } from './dto/dto-validation.interceptor';
import { AllExceptionsFilter } from './filters/all-exceptions.filter';
import { HealthController } from './health.controller';
import { AppHttpModule } from './http/http.module';
import { TraceIdMiddleware } from './middleware/trace-id.middleware';
import { ProxyController } from './proxy/proxy.controller';
import { SseController } from './sse/sse.controller';
import { ResponseTrimMiddleware } from './transform/response-trim.middleware';

/**
 * 根模块（T5.1 / T5.2）。
 * - 全局配置（CORE_URL 等）+ HTTP 客户端（120s 超时、不重试）；
 * - AuthModule 提供全局 AuthGuard（APP_GUARD）与 JWT 验签；
 * - 全局限流 100 req/min·IP（ttl/limit 可配），/healthz 由控制器 @SkipThrottle 豁免；
 * - 全局异常过滤器统一错误体（§6.3 同构）；
 * - traceId 中间件贯穿全路由；
 * - T5.3：DTO 校验拦截器（白名单裁剪/422）、响应裁剪中间件（组合列表/数据状态）、
 *   不可变资源 Cache-Control 中间件、SSE 端点占位（501）。
 */
@Module({
  imports: [
    AppConfigModule,
    AppHttpModule,
    AuthModule,
    ThrottlerModule.forRootAsync({
      imports: [AppConfigModule],
      inject: [AppConfigService],
      useFactory: (config: AppConfigService) => [
        { ttl: config.throttleTtlSeconds * 1000, limit: config.throttleLimit },
      ],
    }),
  ],
  // 顺序即路由优先级：AuthController、SseController 的显式路由必须在 ProxyController
  // （`@All('api/*')` 通配代理）之前注册，优先命中；通配代理无需改动。
  controllers: [HealthController, AuthController, SseController, ProxyController],
  providers: [
    { provide: APP_GUARD, useClass: ThrottlerGuard },
    { provide: APP_INTERCEPTOR, useClass: DtoValidationInterceptor },
    { provide: APP_FILTER, useClass: AllExceptionsFilter },
  ],
})
export class AppModule implements NestModule {
  configure(consumer: MiddlewareConsumer): void {
    // 顺序：traceId → 响应裁剪（包裹 res）→ Cache-Control（设置头），
    // 均在通配代理转发之前生效，且各自内部按路径精确匹配，互不干扰。
    consumer
      .apply(TraceIdMiddleware, ResponseTrimMiddleware, CacheControlMiddleware)
      .forRoutes('*');
  }
}
