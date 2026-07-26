import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';

import { AuthModule } from './modules/auth/auth.module.js';
import { SectorMarketDataModule } from './modules/sector-market-data/sector-market-data.module.js';
import { UserModule } from './modules/user/user.module.js';
import { PlatformConfigModule } from './platform/config/config.module.js';
import { validateEnvironment } from './platform/config/env.validation.js';
import { DatabaseModule } from './platform/database/database.module.js';
import { HealthModule } from './platform/health/health.module.js';
import { RedisModule } from './platform/redis/redis.module.js';

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
    SectorMarketDataModule,
  ],
})
export class AppModule {}
