import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import type { NestExpressApplication } from '@nestjs/platform-express';

import { AppModule } from './app.module.js';
import { configureApi } from './bootstrap/configure-api.js';
import { AppConfigService } from './config/app-config.service.js';

/** 在默认拒绝的 HTTP 安全边界后创建并启动 API 进程。 */
async function bootstrap(): Promise<void> {
  const app = await NestFactory.create<NestExpressApplication>(AppModule, { bufferLogs: true });
  const logger = new Logger('Bootstrap');
  app.useLogger(logger);
  const config = app.get(AppConfigService);

  configureApi(app, config);

  await app.listen(config.port, '0.0.0.0');
  logger.log(`service-api listening on port ${config.port}`);
}

void bootstrap();
