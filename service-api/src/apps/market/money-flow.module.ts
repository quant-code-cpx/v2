import { Module } from '@nestjs/common';

import { DataSyncModule } from '../../data-sync/data-sync.module.js';
import { MoneyFlowController } from './money-flow.controller.js';
import { MoneyFlowService } from './money-flow.service.js';

/** 装配资金流公开 POST API 与 service-data-sync 防腐客户端。 */
@Module({
  imports: [DataSyncModule],
  controllers: [MoneyFlowController],
  providers: [MoneyFlowService],
})
export class MoneyFlowModule {}
