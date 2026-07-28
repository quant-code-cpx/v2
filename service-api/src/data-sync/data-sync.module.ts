import { Module } from '@nestjs/common';

import { AppConfigService } from '../config/app-config.service.js';
import { EquityInstrumentClient } from './clients/equity-instrument.client.js';
import { EquityMarketDataClient } from './clients/equity-market-data.client.js';
import { FinancialDataClient } from './clients/financial-data.client.js';
import { MoneyFlowClient } from './clients/money-flow.client.js';
import { SectorMarketDataClient } from './clients/sector-market-data.client.js';
import { SwSectorClient } from './clients/sw-sector.client.js';

/** 使用集中配置构造证券数据 Client，并保留测试显式传入 `fetch` 替身的能力。 */
function createEquityInstrumentClient(config: AppConfigService): EquityInstrumentClient {
  return new EquityInstrumentClient(config);
}

/** 使用集中配置构造个股市场数据 Client。 */
function createEquityMarketDataClient(config: AppConfigService): EquityMarketDataClient {
  return new EquityMarketDataClient(config);
}

/** 使用集中配置构造财务数据 Client。 */
function createFinancialDataClient(config: AppConfigService): FinancialDataClient {
  return new FinancialDataClient(config);
}

/** 使用集中配置构造资金流数据 Client。 */
function createMoneyFlowClient(config: AppConfigService): MoneyFlowClient {
  return new MoneyFlowClient(config);
}

/** 使用集中配置构造板块数据 Client，并保留测试显式传入 `fetch` 替身的能力。 */
function createSectorMarketDataClient(config: AppConfigService): SectorMarketDataClient {
  return new SectorMarketDataClient(config);
}

/** 使用集中配置构造申万行业数据 Client。 */
function createSwSectorClient(config: AppConfigService): SwSectorClient {
  return new SwSectorClient(config);
}

/** 集中装配 `service-data-sync` 内部读取客户端，不承载同步任务或权威数据。 */
@Module({
  providers: [
    {
      provide: EquityInstrumentClient,
      inject: [AppConfigService],
      useFactory: createEquityInstrumentClient,
    },
    {
      provide: SectorMarketDataClient,
      inject: [AppConfigService],
      useFactory: createSectorMarketDataClient,
    },
    {
      provide: EquityMarketDataClient,
      inject: [AppConfigService],
      useFactory: createEquityMarketDataClient,
    },
    {
      provide: FinancialDataClient,
      inject: [AppConfigService],
      useFactory: createFinancialDataClient,
    },
    {
      provide: MoneyFlowClient,
      inject: [AppConfigService],
      useFactory: createMoneyFlowClient,
    },
    {
      provide: SwSectorClient,
      inject: [AppConfigService],
      useFactory: createSwSectorClient,
    },
  ],
  exports: [
    EquityInstrumentClient,
    EquityMarketDataClient,
    FinancialDataClient,
    MoneyFlowClient,
    SectorMarketDataClient,
    SwSectorClient,
  ],
})
export class DataSyncModule {}
