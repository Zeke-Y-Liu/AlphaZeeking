import { HttpModule, HttpModuleOptions } from '@nestjs/axios';
import { Module } from '@nestjs/common';

import { AppConfigModule } from '../config/config.module';
import { AppConfigService } from '../config/app-config.service';
import { CoreClientService } from './core-client.service';

/**
 * HTTP 客户端模块（T5.1）。
 * 超时 120s（长任务同步代理）、禁止重定向、不自动重试。
 */
@Module({
  imports: [
    HttpModule.registerAsync({
      imports: [AppConfigModule],
      inject: [AppConfigService],
      useFactory: (): HttpModuleOptions => {
        // retry: 0 —— 约定标记：axios 默认即不重试，本模块未注册任何 retry 拦截器。
        const options = {
          timeout: 120_000,
          maxRedirects: 0,
          retry: 0,
        } as unknown as HttpModuleOptions;
        return options;
      },
    }),
  ],
  providers: [CoreClientService],
  exports: [CoreClientService],
})
export class AppHttpModule {}
