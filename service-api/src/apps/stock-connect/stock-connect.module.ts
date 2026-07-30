import { Module } from '@nestjs/common';

import { DataSyncModule } from '../../data-sync/data-sync.module.js';
import { StockConnectController } from './stock-connect.controller.js';
import { StockConnectRateLimitService } from './stock-connect-rate-limit.service.js';
import { StockConnectService } from './stock-connect.service.js';

/** 装配沪深港通公开 POST API、短期安全限流与 data-sync 防腐 Client。 */
@Module({
  imports: [DataSyncModule],
  controllers: [StockConnectController],
  providers: [StockConnectService, StockConnectRateLimitService],
})
export class StockConnectModule {}
