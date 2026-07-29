import { Module } from '@nestjs/common';

import { DataSyncModule } from '../../data-sync/data-sync.module.js';
import { MarketDataAccessController } from './market-data-access.controller.js';
import { MarketDataAccessService } from './market-data-access.service.js';

/** 装配统一市场数据公开 POST 入口及其 data-sync 防腐客户端。 */
@Module({
  imports: [DataSyncModule],
  controllers: [MarketDataAccessController],
  providers: [MarketDataAccessService],
})
export class MarketDataAccessModule {}
