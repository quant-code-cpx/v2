import { BadRequestException } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { EquityInstrumentClient } from '../../../data-sync/clients/equity-instrument.client.js';
import { EquityInstrumentService } from '../equity-instrument.service.js';

/** 创建只实现应用服务当前依赖方法的严格测试替身。 */
function clientDouble(): {
  client: EquityInstrumentClient;
  listEquities: ReturnType<typeof vi.fn>;
  getEquity: ReturnType<typeof vi.fn>;
  listListingStatusHistory: ReturnType<typeof vi.fn>;
} {
  const listEquities = vi.fn();
  const getEquity = vi.fn();
  const listListingStatusHistory = vi.fn();
  return {
    client: {
      listEquities,
      getEquity,
      listListingStatusHistory,
    } as unknown as EquityInstrumentClient,
    listEquities,
    getEquity,
    listListingStatusHistory,
  };
}

/** 覆盖跨字段时间约束和应用层到内部 client 的参数编排。 */
describe('EquityInstrumentService', () => {
  /** 验证未来知识时刻在发起任何内部请求前被拒绝。 */
  it('rejects a future knowledge cutoff before calling the downstream client', () => {
    const dependency = clientDouble();
    const service = new EquityInstrumentService(dependency.client);
    const future = new Date(Date.now() + 60_000).toISOString();

    expect(() =>
      service.listEquities({ knownAt: future, limit: 50 }, undefined, 'req-future'),
    ).toThrow(BadRequestException);
    expect(dependency.listEquities).not.toHaveBeenCalled();
  });

  /** 验证历史窗口采用左闭右开语义，起点不得等于或晚于终点。 */
  it('rejects an empty or reversed listing history range', () => {
    const dependency = clientDouble();
    const service = new EquityInstrumentService(dependency.client);

    expect(() =>
      service.listListingStatusHistory(
        { exchange: 'SSE', symbol: '600000' },
        { effectiveFrom: '2026-07-01', effectiveTo: '2026-07-01', limit: 50 },
        undefined,
        'req-range',
      ),
    ).toThrow(BadRequestException);
    expect(dependency.listListingStatusHistory).not.toHaveBeenCalled();
  });

  /** 验证合法目录查询完整传递重复状态、条件头和请求标识。 */
  it('forwards a valid list request without selecting an arbitrary identity', async () => {
    const dependency = clientDouble();
    dependency.listEquities.mockResolvedValue({ status: 304, etag: '"equities-v1"' });
    const service = new EquityInstrumentService(dependency.client);

    await expect(
      service.listEquities(
        {
          exchange: 'SZSE',
          status: ['LISTED', 'SUSPENDED'],
          asOf: '2026-07-01',
          knownAt: '2026-07-01T00:00:00Z',
          cursor: 'next-page',
          limit: 25,
        },
        '"equities-v0"',
        'req-forward',
      ),
    ).resolves.toEqual({ status: 304, etag: '"equities-v1"' });
    expect(dependency.listEquities).toHaveBeenCalledWith({
      exchange: 'SZSE',
      statuses: ['LISTED', 'SUSPENDED'],
      query: undefined,
      asOf: '2026-07-01',
      knownAt: '2026-07-01T00:00:00Z',
      cursor: 'next-page',
      limit: 25,
      ifNoneMatch: '"equities-v0"',
      requestId: 'req-forward',
    });
  });

  /** 验证无界或异常条件请求头不会被透传给同步服务。 */
  it('rejects an oversized conditional request header', () => {
    const dependency = clientDouble();
    const service = new EquityInstrumentService(dependency.client);

    expect(() =>
      service.getEquity({ exchange: 'BSE', symbol: '430047' }, {}, 'x'.repeat(257), 'req-etag'),
    ).toThrow(BadRequestException);
    expect(dependency.getEquity).not.toHaveBeenCalled();
  });
});
