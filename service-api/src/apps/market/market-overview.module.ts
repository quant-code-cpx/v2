import { Module } from '@nestjs/common';

import { DataSyncModule } from '../../data-sync/data-sync.module.js';
import { MarketOverviewController } from './market-overview.controller.js';
import { MarketOverviewService } from './market-overview.service.js';

/** 装配市场完整包与新增行业读取，权威数据仍只由 service-data-sync 持有。 */
@Module({
  imports: [DataSyncModule],
  controllers: [MarketOverviewController],
  providers: [MarketOverviewService],
})
export class MarketOverviewModule {}
