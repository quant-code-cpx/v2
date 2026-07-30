import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { EquityMarketDataController } from '../equity-market-data.controller.js';
import type { EquityMarketDataService } from '../equity-market-data.service.js';

const dataVersion = '00000000-0000-4000-8000-000000000001';

/** 覆盖四个公开 POST 端点的应用服务委派与条件响应映射。 */
describe('EquityMarketDataController', () => {
  /** 验证行情、因子、事件和概况方法均保留请求关联标识。 */
  it('delegates all market-data reads and maps conditional responses', async () => {
    const marketData = {
      listBars: vi.fn().mockResolvedValue({ status: 304, etag: '"bars"', dataVersion }),
      listAdjustmentFactors: vi
        .fn()
        .mockResolvedValue({ status: 304, etag: '"factors"', dataVersion }),
      listCorporateActions: vi
        .fn()
        .mockResolvedValue({ status: 304, etag: '"actions"', dataVersion }),
      getCompanyProfile: vi.fn().mockResolvedValue({ status: 304, etag: '"profile"', dataVersion }),
    };
    const controller = new EquityMarketDataController(
      marketData as unknown as EquityMarketDataService,
    );
    const path = { exchange: 'SSE', symbol: '600519' } as never;
    const request = { requestId: 'req-market' } as never;

    await controller.listBars(path, {} as never, undefined, request, response().value);
    await controller.listAdjustmentFactors(path, {} as never, undefined, request, response().value);
    await controller.listCorporateActions(path, {} as never, undefined, request, response().value);
    await controller.getCompanyProfile(
      path,
      { dataVersion, asOf: '2026-07-28' },
      undefined,
      request,
      response().value,
    );

    expect(marketData.listBars).toHaveBeenCalledWith(path, {}, undefined, 'req-market');
    expect(marketData.listAdjustmentFactors).toHaveBeenCalledWith(
      path,
      {},
      undefined,
      'req-market',
    );
    expect(marketData.listCorporateActions).toHaveBeenCalledWith(path, {}, undefined, 'req-market');
    expect(marketData.getCompanyProfile).toHaveBeenCalledWith(
      path,
      { dataVersion, asOf: '2026-07-28' },
      undefined,
      'req-market',
    );
  });
});

/** 构造条件响应映射所需的最小 Express 响应。 */
function response(): { value: Response } {
  const send = vi.fn();
  const status = vi.fn().mockReturnValue({ send });
  return {
    value: { setHeader: vi.fn(), status } as never,
  };
}
