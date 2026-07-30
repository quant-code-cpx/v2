import { describe, expect, it, vi } from 'vitest';

import type { EquityWorkspaceClient } from '../../../data-sync/clients/equity-workspace.client.js';
import {
  EQUITY_DATASET_FAMILIES,
  EQUITY_TRADING_STATUSES,
} from '../../../data-sync/contracts/equity-workspace.contract.js';
import type { EquityWorkspaceRateLimitService } from '../equity-workspace-rate-limit.service.js';
import { EquityWorkspaceService } from '../equity-workspace.service.js';

/** 覆盖股票中心应用层 DTO、跨字段约束、限流和内部请求映射。 */
describe('EquityWorkspaceService', () => {
  /** 搜索应把 listingStatuses 映射为内部 lifecycleStatuses 并绑定用户限流。 */
  it('validates and delegates discovery search', async () => {
    const fixture = serviceFixture();
    await fixture.service.search(
      {
        q: '600519',
        listingStatuses: ['LISTED'],
        sort: [{ field: 'symbol', direction: 'ASC' }],
        limit: 50,
      },
      '"older"',
      'req-search',
      'user-1',
    );

    expect(fixture.rateLimit.assertSearchAllowed).toHaveBeenCalledWith(
      'user-1',
      expect.objectContaining({ limit: 50 }),
    );
    expect(fixture.client.search).toHaveBeenCalledWith({
      body: {
        q: '600519',
        lifecycleStatuses: ['LISTED'],
        sort: [{ field: 'symbol', direction: 'ASC' }],
        limit: 50,
      },
      ifNoneMatch: '"older"',
      requestId: 'req-search',
    });
  });

  /** 公开 DTO 必须接受全部冻结交易状态并原样交给内部查询。 */
  it('delegates every frozen trading status in one search', async () => {
    const fixture = serviceFixture();

    await fixture.service.search(
      { tradingStatuses: [...EQUITY_TRADING_STATUSES], limit: 50 },
      undefined,
      'req-all-trading-statuses',
      'user-1',
    );

    expect(fixture.client.search).toHaveBeenCalledWith({
      body: {
        lifecycleStatuses: ['LISTED'],
        tradingStatuses: [...EQUITY_TRADING_STATUSES],
        limit: 50,
      },
      ifNoneMatch: undefined,
      requestId: 'req-all-trading-statuses',
    });
  });

  /** 研究态资金流不得被接受为可筛选或可排序的股票发现能力。 */
  it('rejects money-flow discovery while the production methodology is unavailable', async () => {
    const requests = [
      { sort: [{ field: 'moneyFlowNetAmount', direction: 'DESC' }] },
      { columns: ['symbol', 'moneyFlowNetRatio'] },
      {
        moneyFlow: {
          methodology: { code: 'eastmoney-order-size' },
          bucket: 'MAIN',
          range: { min: '1' },
        },
      },
    ];
    for (const [index, request] of requests.entries()) {
      const fixture = serviceFixture();
      await expect(
        fixture.service.search(request, undefined, `req-money-flow-${String(index)}`, 'user-1'),
      ).rejects.toMatchObject({
        status: 409,
        response: { code: 'capability-unavailable' },
      });
      expect(fixture.client.search).not.toHaveBeenCalled();
      expect(fixture.rateLimit.assertSearchAllowed).not.toHaveBeenCalled();
    }
  });

  /** 暂停上市没有已验证 producer 时必须拒绝显式筛选，不能返回误导性空集。 */
  it('rejects suspended listing discovery while lifecycle coverage is unavailable', async () => {
    const fixture = serviceFixture();

    await expect(
      fixture.service.search(
        { listingStatuses: ['LISTED', 'SUSPENDED'] },
        undefined,
        'req-suspended-lifecycle',
        'user-1',
      ),
    ).rejects.toMatchObject({
      status: 409,
      response: { code: 'capability-unavailable' },
    });
    expect(fixture.client.search).not.toHaveBeenCalled();
    expect(fixture.rateLimit.assertSearchAllowed).not.toHaveBeenCalled();
  });

  /** 事件窗口必须显式提供两端且不得超过十年，coverage 不承诺无界历史。 */
  it('rejects invalid event windows before downstream access', async () => {
    const fixture = serviceFixture();

    for (const [index, request] of [{}, { start: '2026-01-01' }].entries()) {
      await expect(
        fixture.service.searchEvents(
          { exchange: 'SSE', symbol: '600519' },
          request,
          undefined,
          `req-events-${String(index)}`,
          'user-1',
        ),
      ).rejects.toMatchObject({
        status: 400,
        response: { code: 'validation-error' },
      });
    }
    expect(fixture.client.searchEvents).not.toHaveBeenCalled();
    expect(fixture.rateLimit.assertEventsAllowed).not.toHaveBeenCalled();
  });

  /** 事件身份日期应独立于筛选窗口，并原样传给双时态内部 reader。 */
  it('delegates the event identity as-of anchor', async () => {
    const fixture = serviceFixture();
    await fixture.service.searchEvents(
      { exchange: 'SSE', symbol: '600519' },
      {
        families: ['DRAGON_TIGER'],
        asOf: '2026-07-29',
        start: '2026-01-01',
        end: '2026-07-29',
        limit: 50,
      },
      undefined,
      'req-events-as-of',
      'user-1',
    );

    expect(fixture.client.searchEvents).toHaveBeenCalledWith({
      exchange: 'SSE',
      symbol: '600519',
      body: {
        families: ['DRAGON_TIGER'],
        asOf: '2026-07-29',
        start: '2026-01-01',
        end: '2026-07-29',
        limit: 50,
      },
      ifNoneMatch: undefined,
      requestId: 'req-events-as-of',
    });
  });

  /** 数据状态应在一次请求中允许全部十八个冻结数据族。 */
  it('delegates all data-status families in one request', async () => {
    const fixture = serviceFixture();
    await fixture.service.getDataStatus(
      { exchange: 'SSE', symbol: '600519' },
      { families: [...EQUITY_DATASET_FAMILIES] },
      undefined,
      'req-status',
      'user-1',
    );

    expect(fixture.rateLimit.assertDataStatusAllowed).toHaveBeenCalledWith('user-1');
    expect(fixture.client.getDataStatus).toHaveBeenCalledWith({
      exchange: 'SSE',
      symbol: '600519',
      body: { families: [...EQUITY_DATASET_FAMILIES] },
      ifNoneMatch: undefined,
      requestId: 'req-status',
    });
  });
});

/** 构造不访问网络或 Redis 的应用服务替身。 */
function serviceFixture() {
  const search = vi
    .fn<EquityWorkspaceClient['search']>()
    .mockResolvedValue({ status: 200, etag: undefined, dataVersion: undefined, body: {} as never });
  const searchEvents = vi
    .fn<EquityWorkspaceClient['searchEvents']>()
    .mockResolvedValue({ status: 200, etag: undefined, dataVersion: undefined, body: {} as never });
  const getDataStatus = vi
    .fn<EquityWorkspaceClient['getDataStatus']>()
    .mockResolvedValue({ status: 200, etag: undefined, dataVersion: undefined, body: {} as never });
  const client = {
    search,
    searchEvents,
    getDataStatus,
  };
  const rateLimit = {
    assertSearchAllowed: vi.fn().mockResolvedValue(undefined),
    assertEventsAllowed: vi.fn().mockResolvedValue(undefined),
    assertDataStatusAllowed: vi.fn().mockResolvedValue(undefined),
  };
  return {
    client,
    rateLimit,
    service: new EquityWorkspaceService(
      client as unknown as EquityWorkspaceClient,
      rateLimit as unknown as EquityWorkspaceRateLimitService,
    ),
  };
}
