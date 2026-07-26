import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { DocumentBuilder, SwaggerModule } from '@nestjs/swagger';
import type { NestExpressApplication } from '@nestjs/platform-express';

import { AppModule } from './app.module.js';
import { AppConfigService } from './platform/config/app-config.service.js';
import { configureHttp } from './platform/http/configure-http.js';

async function bootstrap(): Promise<void> {
  const app = await NestFactory.create<NestExpressApplication>(AppModule, { bufferLogs: true });
  const logger = new Logger('Bootstrap');
  app.useLogger(logger);
  const config = app.get(AppConfigService);

  configureHttp(app, config);

  const document = SwaggerModule.createDocument(
    app,
    new DocumentBuilder().setTitle('quant-v2 API').setVersion('1.0').addBearerAuth().build(),
  );
  SwaggerModule.setup('openapi', app, document, { jsonDocumentUrl: 'openapi-json' });

  await app.listen(config.port, '0.0.0.0');
  logger.log(`service-api listening on port ${config.port}`);
}

void bootstrap();
