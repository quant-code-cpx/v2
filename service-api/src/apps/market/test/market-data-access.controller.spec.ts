import { describe, expect, it, vi } from 'vitest';

import { MarketDataAccessController } from '../market-data-access.controller.js';
import type { MarketDataAccessService } from '../market-data-access.service.js';

/** 覆盖通用市场数据公开 POST 路由的安全委派。 */
describe('MarketDataAccessController', () => {
  /** 请求标识和未经修改的查询体必须传给应用服务，空页不在 Controller 层改写。 */
  it('delegates a query and preserves the empty response', async () => {
    const expected = {
      meta: { availability: 'SOURCE_UNAVAILABLE' },
      records: [],
    };
    const marketData = { query: vi.fn().mockResolvedValue(expected) };
    const controller = new MarketDataAccessController(
      marketData as unknown as MarketDataAccessService,
    );
    const body = { dataset: { code: 'derivative.bar.1d.reported', schemaVersion: 1 } };

    const result = await controller.query(body, { requestId: 'req-market-data' } as never);

    expect(marketData.query).toHaveBeenCalledWith(body, 'req-market-data');
    expect(result).toBe(expected);
  });
});
