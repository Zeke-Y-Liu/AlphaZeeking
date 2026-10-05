import { Global, Module } from '@nestjs/common';
import { ConfigModule as NestConfigModule } from '@nestjs/config';

import { AppConfigService } from './app-config.service';
import { bffConfiguration } from './configuration';

/**
 * 全局配置模块（T5.1）。
 * - envFilePath 指向仓库根 ../.env（相对 bff 工作目录）；文件不存在时静默使用缺省值，不报错。
 * - isGlobal 使 ConfigService 全局可注入。
 */
@Global()
@Module({
  imports: [
    NestConfigModule.forRoot({
      isGlobal: true,
      envFilePath: ['../.env'],
      load: [bffConfiguration],
    }),
  ],
  providers: [AppConfigService],
  exports: [AppConfigService],
})
export class AppConfigModule {}
