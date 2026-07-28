import { Module } from '@nestjs/common';

import { AppConfigService } from '../config/app-config.service.js';
import { EquityInstrumentClient } from './clients/equity-instrument.client.js';
import { SectorMarketDataClient } from './clients/sector-market-data.client.js';

/** 使用集中配置构造证券数据 Client，并保留测试显式传入 `fetch` 替身的能力。 */
function createEquityInstrumentClient(config: AppConfigService): EquityInstrumentClient {
  return new EquityInstrumentClient(config);
}

/** 使用集中配置构造板块数据 Client，并保留测试显式传入 `fetch` 替身的能力。 */
function createSectorMarketDataClient(config: AppConfigService): SectorMarketDataClient {
  return new SectorMarketDataClient(config);
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
  ],
  exports: [EquityInstrumentClient, SectorMarketDataClient],
})
export class DataSyncModule {}
