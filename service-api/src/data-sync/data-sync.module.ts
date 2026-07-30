import { Module } from '@nestjs/common';

import { AppConfigService } from '../config/app-config.service.js';
import { DataOperationsClient } from './clients/data-operations.client.js';
import { EquityInstrumentClient } from './clients/equity-instrument.client.js';
import { EquityMarketDataClient } from './clients/equity-market-data.client.js';
import { EquityWorkspaceClient } from './clients/equity-workspace.client.js';
import { FinancialDataClient } from './clients/financial-data.client.js';
import { MoneyFlowClient } from './clients/money-flow.client.js';
import { MarketDataAccessClient } from './clients/market-data-access.client.js';
import { MarketOverviewClient } from './clients/market-overview.client.js';
import { SectorMarketDataClient } from './clients/sector-market-data.client.js';
import { StockConnectClient } from './clients/stock-connect.client.js';
import { SwSectorClient } from './clients/sw-sector.client.js';

/** 使用集中配置构造证券数据 Client，并保留测试显式传入 `fetch` 替身的能力。 */
function createEquityInstrumentClient(config: AppConfigService): EquityInstrumentClient {
  return new EquityInstrumentClient(config);
}

/** 使用集中配置构造个股市场数据 Client。 */
function createEquityMarketDataClient(config: AppConfigService): EquityMarketDataClient {
  return new EquityMarketDataClient(config);
}

/** 使用集中配置构造股票中心 Client。 */
function createEquityWorkspaceClient(config: AppConfigService): EquityWorkspaceClient {
  return new EquityWorkspaceClient(config);
}

/** 使用集中配置构造财务数据 Client。 */
function createFinancialDataClient(config: AppConfigService): FinancialDataClient {
  return new FinancialDataClient(config);
}

/** 使用集中配置构造资金流数据 Client。 */
function createMoneyFlowClient(config: AppConfigService): MoneyFlowClient {
  return new MoneyFlowClient(config);
}

/** 使用集中配置构造通用市场数据 POST Client。 */
function createMarketDataAccessClient(config: AppConfigService): MarketDataAccessClient {
  return new MarketDataAccessClient(config);
}

/** 使用集中配置构造市场完整包与新增排行 Client。 */
function createMarketOverviewClient(config: AppConfigService): MarketOverviewClient {
  return new MarketOverviewClient(config);
}

/** 使用集中配置构造板块数据 Client，并保留测试显式传入 `fetch` 替身的能力。 */
function createSectorMarketDataClient(config: AppConfigService): SectorMarketDataClient {
  return new SectorMarketDataClient(config);
}

/** 使用集中配置构造申万行业数据 Client。 */
function createSwSectorClient(config: AppConfigService): SwSectorClient {
  return new SwSectorClient(config);
}

/** 使用集中配置构造数据运维内部 POST Client。 */
function createDataOperationsClient(config: AppConfigService): DataOperationsClient {
  return new DataOperationsClient(config);
}

/** 使用集中配置构造沪深港通专用只读 POST Client。 */
function createStockConnectClient(config: AppConfigService): StockConnectClient {
  return new StockConnectClient(config);
}

/** 集中装配 `service-data-sync` 内部 HTTP 客户端，不承载同步任务或权威数据。 */
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
      provide: EquityWorkspaceClient,
      inject: [AppConfigService],
      useFactory: createEquityWorkspaceClient,
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
      provide: MarketDataAccessClient,
      inject: [AppConfigService],
      useFactory: createMarketDataAccessClient,
    },
    {
      provide: MarketOverviewClient,
      inject: [AppConfigService],
      useFactory: createMarketOverviewClient,
    },
    {
      provide: SwSectorClient,
      inject: [AppConfigService],
      useFactory: createSwSectorClient,
    },
    {
      provide: DataOperationsClient,
      inject: [AppConfigService],
      useFactory: createDataOperationsClient,
    },
    {
      provide: StockConnectClient,
      inject: [AppConfigService],
      useFactory: createStockConnectClient,
    },
  ],
  exports: [
    EquityInstrumentClient,
    EquityMarketDataClient,
    EquityWorkspaceClient,
    FinancialDataClient,
    MoneyFlowClient,
    MarketDataAccessClient,
    MarketOverviewClient,
    SectorMarketDataClient,
    SwSectorClient,
    DataOperationsClient,
    StockConnectClient,
  ],
})
export class DataSyncModule {}
