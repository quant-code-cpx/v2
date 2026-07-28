import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';

import { AuthModule } from './apps/auth/auth.module.js';
import { AuditModule } from './apps/audit/audit.module.js';
import { IndustryModule } from './apps/industry/industry.module.js';
import { MoneyFlowModule } from './apps/market/money-flow.module.js';
import { StockModule } from './apps/stock/stock.module.js';
import { UserModule } from './apps/user/user.module.js';
import { AppConfigModule } from './config/app-config.module.js';
import { validateEnvironment } from './config/env.validation.js';
import { DatabaseModule } from './shared/database/database.module.js';
import { HealthModule } from './shared/health/health.module.js';
import { RedisModule } from './shared/redis/redis.module.js';

/** 组合 API 服务的基础设施与业务模块，保持模块依赖由根模块单向装配。 */
@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      validate: validateEnvironment,
      cache: true,
    }),
    AppConfigModule,
    DatabaseModule,
    RedisModule,
    HealthModule,
    UserModule,
    AuthModule,
    AuditModule,
    StockModule,
    IndustryModule,
    MoneyFlowModule,
  ],
})
export class AppModule {}
