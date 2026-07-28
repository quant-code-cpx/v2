import { BadRequestException } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { EquityMarketDataClient } from '../../../data-sync/clients/equity-market-data.client.js';
import { EquityMarketDataService } from '../equity-market-data.service.js';

/** 覆盖公开参数组合校验与 client 转发。 */
describe('EquityMarketDataService', () => {
  /** 验证周线复权查询完整转发交易所限定身份和请求关联。 */
  it('forwards a direct weekly adjusted bar query', async () => {
    const listBars = vi.fn().mockResolvedValue({ status: 304, etag: '"same"' });
    const service = new EquityMarketDataService({
      listBars,
    } as unknown as EquityMarketDataClient);

    const result = await service.listBars(
      { exchange: 'SSE', symbol: '600519' },
      {
        period: '1w',
        start: '2026-01-01',
        end: '2026-07-28',
        adjust: 'qfq',
        adjustAsOf: '2026-07-28',
        cursor: 'next-page',
        limit: 500,
      },
      '"same"',
      'req-bars',
    );

    expect(result.status).toBe(304);
    expect(listBars).toHaveBeenCalledWith({
      exchange: 'SSE',
      symbol: '600519',
      period: '1w',
      start: '2026-01-01',
      end: '2026-07-28',
      adjust: 'qfq',
      adjustAsOf: '2026-07-28',
      cursor: 'next-page',
      limit: 500,
      ifNoneMatch: '"same"',
      requestId: 'req-bars',
    });
  });

  /** 验证三类参考数据调用各自 client 方法。 */
  it('forwards factor action and profile reads', async () => {
    const listAdjustmentFactors = vi.fn().mockResolvedValue({ status: 304 });
    const listCorporateActions = vi.fn().mockResolvedValue({ status: 304 });
    const getCompanyProfile = vi.fn().mockResolvedValue({ status: 304 });
    const client = {
      listAdjustmentFactors,
      listCorporateActions,
      getCompanyProfile,
    } as unknown as EquityMarketDataClient;
    const service = new EquityMarketDataService(client);
    const path = { exchange: 'SSE' as const, symbol: '600519' };

    await service.listAdjustmentFactors(
      path,
      { start: '2026-01-01', end: '2026-07-28', limit: 500 },
      undefined,
      'req-factor',
    );
    await service.listCorporateActions(
      path,
      { start: '2025-01-01', end: '2026-07-28', limit: 100 },
      undefined,
      'req-action',
    );
    await service.getCompanyProfile(path, undefined, 'req-profile');

    expect(listAdjustmentFactors).toHaveBeenCalledOnce();
    expect(listCorporateActions).toHaveBeenCalledOnce();
    expect(getCompanyProfile).toHaveBeenCalledOnce();
  });

  /** 验证倒置日期、无复权锚点和过长 ETag 在访问下游前被拒绝。 */
  it('rejects invalid cross-field queries before calling the client', () => {
    const listBars = vi.fn();
    const service = new EquityMarketDataService({
      listBars,
    } as unknown as EquityMarketDataClient);
    const path = { exchange: 'SSE' as const, symbol: '600519' };

    expect(() =>
      service.listBars(
        path,
        {
          period: '1d',
          start: '2026-07-28',
          end: '2026-01-01',
          adjust: 'none',
          limit: 500,
        },
        undefined,
        'req-range',
      ),
    ).toThrow(BadRequestException);
    expect(() =>
      service.listBars(
        path,
        {
          period: '1d',
          start: '2026-01-01',
          end: '2026-07-28',
          adjust: 'none',
          adjustAsOf: '2026-07-28',
          limit: 500,
        },
        undefined,
        'req-anchor',
      ),
    ).toThrow(BadRequestException);
    expect(() => service.getCompanyProfile(path, 'x'.repeat(257), 'req-etag')).toThrow(
      BadRequestException,
    );
    expect(listBars).not.toHaveBeenCalled();
  });
});
