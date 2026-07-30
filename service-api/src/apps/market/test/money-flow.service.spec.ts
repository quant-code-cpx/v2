import { BadRequestException } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { MoneyFlowClient } from '../../../data-sync/clients/money-flow.client.js';
import { MoneyFlowService } from '../money-flow.service.js';

const dataVersion = '00000000-0000-4000-8000-000000000017';

/** 覆盖股票中心个股资金流的 publication 门禁。 */
describe('MoneyFlowService equity publication binding', () => {
  /** 验证个股序列必须携带精确版本，并完整传给内部防腐 client。 */
  it('requires and forwards the status dataVersion for equity series', async () => {
    const listDaily = vi.fn().mockResolvedValue({ status: 304 });
    const service = new MoneyFlowService({ listDaily } as unknown as MoneyFlowClient);
    const path = {
      methodologyId: 'eastmoney.trade-direction',
      exchange: 'SSE',
      symbol: '600519',
    } as never;
    const query = {
      dataVersion,
      methodologyVersion: '1',
      bucket: 'main',
      start: '2026-07-01',
      end: '2026-07-28',
      limit: 200,
    } as never;

    await service.listEquityDaily(path, query, undefined, 'req-equity-money-flow');

    expect(listDaily).toHaveBeenCalledWith(
      expect.objectContaining({
        dataVersion,
        scopePath: 'equities/SSE/600519',
        requestId: 'req-equity-money-flow',
      }),
    );
  });

  /** 验证缺失版本在访问 data-sync 前即被拒绝。 */
  it('rejects an unpinned equity series', () => {
    const listDaily = vi.fn();
    const service = new MoneyFlowService({ listDaily } as unknown as MoneyFlowClient);

    expect(() =>
      service.listEquityDaily(
        {
          methodologyId: 'eastmoney.trade-direction',
          exchange: 'SSE',
          symbol: '600519',
        } as never,
        {
          methodologyVersion: '1',
          bucket: 'main',
          start: '2026-07-01',
          end: '2026-07-28',
          limit: 200,
        },
        undefined,
        'req-unpinned-money-flow',
      ),
    ).toThrow(BadRequestException);
    expect(listDaily).not.toHaveBeenCalled();
  });
});
