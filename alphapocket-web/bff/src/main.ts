import { NestFactory } from '@nestjs/core';

import { AppModule } from './app.module';
import { configureCors } from './auth/cors';
import { AppConfigService } from './config/app-config.service';

/**
 * BFF 入口（T5.1）。
 * 端口由配置提供（PORT，缺省 8081，见实施书 §3.2）；CORS 允许 dev 源（T5.2）。
 */
async function bootstrap(): Promise<void> {
  const app = await NestFactory.create(AppModule);
  configureCors(app);
  const config = app.get(AppConfigService);
  await app.listen(config.port);
  // eslint-disable-next-line no-console
  console.log(`[bff] listening on http://localhost:${config.port}`);
}

void bootstrap();
