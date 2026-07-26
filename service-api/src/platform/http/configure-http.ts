import { ValidationPipe } from '@nestjs/common';
import type { NestExpressApplication } from '@nestjs/platform-express';

import type { AppConfigService } from '../config/app-config.service.js';
import { ProblemDetailsFilter } from './problem-details.filter.js';
import { requestIdMiddleware } from './request-id.middleware.js';

/** Apply shared HTTP trust, CORS, validation, request-id, and error-response policy. */
export function configureHttp(app: NestExpressApplication, config: AppConfigService): void {
  app.set('trust proxy', config.trustProxy);
  app.setGlobalPrefix(config.apiPrefix, { exclude: ['health', 'ready'] });
  app.enableCors({
    origin: config.corsOrigin,
    credentials: true,
    methods: ['GET', 'POST', 'PATCH', 'DELETE'],
    allowedHeaders: ['Authorization', 'Content-Type', 'If-Match', 'X-Request-Id'],
    exposedHeaders: ['ETag', 'Location', 'Retry-After', 'X-Request-Id'],
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
