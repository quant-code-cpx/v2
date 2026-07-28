import { ValidationPipe } from '@nestjs/common';
import type { NestExpressApplication } from '@nestjs/platform-express';

import type { AppConfigService } from '../config/app-config.service.js';
import { ProblemDetailsFilter } from '../lifecycle/filters/problem-details.filter.js';
import { requestIdMiddleware } from '../lifecycle/middleware/request-id.middleware.js';

/** 配置 REST API 共用的验证、跨域、请求标识与错误响应策略。 */
export function configureApi(app: NestExpressApplication, config: AppConfigService): void {
  app.set('trust proxy', config.trustProxy);
  app.setGlobalPrefix(config.apiPrefix, { exclude: ['health', 'ready'] });
  app.enableCors({
    origin: config.corsOrigin,
    credentials: true,
    methods: ['POST'],
    allowedHeaders: ['Authorization', 'Content-Type', 'If-Match', 'If-None-Match', 'X-Request-Id'],
    exposedHeaders: ['ETag', 'Location', 'Retry-After', 'X-Data-Version', 'X-Request-Id'],
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
