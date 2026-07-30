import { ConfigModule } from '@nestjs/config';
import { Test } from '@nestjs/testing';
import { describe, expect, it } from 'vitest';

import { AppConfigModule } from '../../config/app-config.module.js';
import { EquityInstrumentClient } from '../clients/equity-instrument.client.js';
import { EquityMarketDataClient } from '../clients/equity-market-data.client.js';
import { EquityWorkspaceClient } from '../clients/equity-workspace.client.js';
import { FinancialDataClient } from '../clients/financial-data.client.js';
import { MoneyFlowClient } from '../clients/money-flow.client.js';
import { MarketDataAccessClient } from '../clients/market-data-access.client.js';
import { MarketOverviewClient } from '../clients/market-overview.client.js';
import { SectorMarketDataClient } from '../clients/sector-market-data.client.js';
import { StockConnectClient } from '../clients/stock-connect.client.js';
import { SwSectorClient } from '../clients/sw-sector.client.js';
import { DataSyncModule } from '../data-sync.module.js';

/** 验证同步服务只读 Client 能由 Nest 模块稳定装配。 */
describe('DataSyncModule', () => {
  /** 显式工厂应阻止 Nest 把 Client 的可选 `fetch` 参数识别为依赖。 */
  it('resolves all internal clients through explicit factories', async () => {
    const moduleReference = await Test.createTestingModule({
      imports: [ConfigModule.forRoot({ isGlobal: true }), AppConfigModule, DataSyncModule],
    }).compile();

    expect(moduleReference.get(EquityInstrumentClient)).toBeInstanceOf(EquityInstrumentClient);
    expect(moduleReference.get(EquityMarketDataClient)).toBeInstanceOf(EquityMarketDataClient);
    expect(moduleReference.get(EquityWorkspaceClient)).toBeInstanceOf(EquityWorkspaceClient);
    expect(moduleReference.get(FinancialDataClient)).toBeInstanceOf(FinancialDataClient);
    expect(moduleReference.get(MoneyFlowClient)).toBeInstanceOf(MoneyFlowClient);
    expect(moduleReference.get(MarketDataAccessClient)).toBeInstanceOf(MarketDataAccessClient);
    expect(moduleReference.get(MarketOverviewClient)).toBeInstanceOf(MarketOverviewClient);
    expect(moduleReference.get(SectorMarketDataClient)).toBeInstanceOf(SectorMarketDataClient);
    expect(moduleReference.get(StockConnectClient)).toBeInstanceOf(StockConnectClient);
    expect(moduleReference.get(SwSectorClient)).toBeInstanceOf(SwSectorClient);

    await moduleReference.close();
  });
});
