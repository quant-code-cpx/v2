import { ValidationPipe } from '@nestjs/common';
import type { NestExpressApplication } from '@nestjs/platform-express';

import type { AppConfigService } from '../config/app-config.service.js';
import { ProblemDetailsFilter } from './problem-details.filter.js';
import { requestIdMiddleware } from './request-id.middleware.js';

export function configureHttp(app: NestExpressApplication, config: AppConfigService): void {
  app.set('trust proxy', config.trustProxy);
  app.setGlobalPrefix(config.apiPrefix, { exclude: ['health', 'ready'] });
  app.enableCors({
    origin: config.corsOrigin,
    credentials: true,
    methods: ['GET', 'POST', 'PATCH'],
    allowedHeaders: ['Authorization', 'Content-Type', 'X-Request-Id'],
  });
  app.use(requestIdMiddleware);
  app.useGlobalPipes(
    new ValidationPipe({
      transform: true,
      whitelist: true,
      forbidNonWhitelisted: true,
      transformOptions: { enableImplicitConversion: false },
    }),
  );
  app.useGlobalFilters(new ProblemDetailsFilter());
}
