import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';

import { AuthModule } from './modules/auth/auth.module.js';
import { EquityInstrumentModule } from './modules/equity-instrument/equity-instrument.module.js';
import { SectorMarketDataModule } from './modules/sector-market-data/sector-market-data.module.js';
import { UserModule } from './modules/user/user.module.js';
import { PlatformConfigModule } from './platform/config/config.module.js';
import { validateEnvironment } from './platform/config/env.validation.js';
import { DatabaseModule } from './platform/database/database.module.js';
import { HealthModule } from './platform/health/health.module.js';
import { RedisModule } from './platform/redis/redis.module.js';

/** 组合 API 服务的基础设施与业务模块，保持模块依赖由根模块单向装配。 */
@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      validate: validateEnvironment,
      cache: true,
    }),
    PlatformConfigModule,
    DatabaseModule,
    RedisModule,
    HealthModule,
    UserModule,
    AuthModule,
    EquityInstrumentModule,
    SectorMarketDataModule,
  ],
})
export class AppModule {}
