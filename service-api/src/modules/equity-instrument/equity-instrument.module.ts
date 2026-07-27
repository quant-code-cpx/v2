import { Module } from '@nestjs/common';

import { EquityInstrumentClient } from './equity-instrument.client.js';
import { EquityInstrumentController } from './equity-instrument.controller.js';
import { EquityInstrumentService } from './equity-instrument.service.js';

/** 封装证券主数据防腐读取，禁止持久化同步服务的权威数据。 */
@Module({
  controllers: [EquityInstrumentController],
  providers: [EquityInstrumentClient, EquityInstrumentService],
})
export class EquityInstrumentModule {}
