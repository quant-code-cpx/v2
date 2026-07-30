import { Module } from '@nestjs/common';

import { DataSyncModule } from '../../data-sync/data-sync.module.js';
import { EquityInstrumentController } from './equity-instrument.controller.js';
import { EquityInstrumentService } from './equity-instrument.service.js';
import { EquityMarketDataController } from './equity-market-data.controller.js';
import { EquityMarketDataService } from './equity-market-data.service.js';
import { EquityWorkspaceController } from './equity-workspace.controller.js';
import { EquityWorkspaceRateLimitService } from './equity-workspace-rate-limit.service.js';
import { EquityWorkspaceService } from './equity-workspace.service.js';
import { FinancialDataController } from './financial-data.controller.js';
import { FinancialDataService } from './financial-data.service.js';

/** 封装证券主数据防腐读取，禁止持久化同步服务的权威数据。 */
@Module({
  imports: [DataSyncModule],
  controllers: [
    EquityWorkspaceController,
    EquityInstrumentController,
    EquityMarketDataController,
    FinancialDataController,
  ],
  providers: [
    EquityWorkspaceService,
    EquityWorkspaceRateLimitService,
    EquityInstrumentService,
    EquityMarketDataService,
    FinancialDataService,
  ],
})
export class StockModule {}
