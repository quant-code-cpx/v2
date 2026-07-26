import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import type { NestExpressApplication } from '@nestjs/platform-express';

import { AppModule } from './app.module.js';
import { AppConfigService } from './platform/config/app-config.service.js';
import { configureHttp } from './platform/http/configure-http.js';

/** Create and expose configured API process behind the default-deny HTTP security boundary. */
async function bootstrap(): Promise<void> {
  const app = await NestFactory.create<NestExpressApplication>(AppModule, { bufferLogs: true });
  const logger = new Logger('Bootstrap');
  app.useLogger(logger);
  const config = app.get(AppConfigService);

  configureHttp(app, config);

  await app.listen(config.port, '0.0.0.0');
  logger.log(`service-api listening on port ${config.port}`);
}

void bootstrap();
