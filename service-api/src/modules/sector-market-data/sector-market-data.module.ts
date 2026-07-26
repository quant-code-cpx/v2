import { Module } from '@nestjs/common';

import { SectorMarketDataClient } from './sector-market-data.client.js';
import { SectorMarketDataController } from './sector-market-data.controller.js';
import { SectorMarketDataService } from './sector-market-data.service.js';

/** 封装服务间板块数据读取，禁止此模块持久化同步服务的权威数据。 */
@Module({
  controllers: [SectorMarketDataController],
  providers: [SectorMarketDataClient, SectorMarketDataService],
})
export class SectorMarketDataModule {}
