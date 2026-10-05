import { Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { JwtModule } from '@nestjs/jwt';

import { AppConfigModule } from '../config/config.module';
import { AppConfigService } from '../config/app-config.service';
import { AppHttpModule } from '../http/http.module';
import { AuthGuard } from './auth.guard';

/**
 * 认证模块（T5.2）。
 * - JwtModule 使用 `AP_JWT_SECRET`（HS256）供 AuthGuard 本地验签；
 * - AuthGuard 以 APP_GUARD 注册为全局守卫（AuthModule 被 AppModule 导入即生效）；
 * - AuthController 由 AppModule 显式注册在 ProxyController 之前（保证路由优先级），
 *   故本模块不声明 controllers。
 */
@Module({
  imports: [
    AppHttpModule,
    JwtModule.registerAsync({
      imports: [AppConfigModule],
      inject: [AppConfigService],
      useFactory: (config: AppConfigService) => ({
        secret: config.jwtSecret,
        signOptions: { algorithm: 'HS256' },
        verifyOptions: { algorithms: ['HS256'] },
      }),
    }),
  ],
  providers: [AuthGuard, { provide: APP_GUARD, useClass: AuthGuard }],
  exports: [AuthGuard],
})
export class AuthModule {}
