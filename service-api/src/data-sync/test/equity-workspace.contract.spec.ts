import { describe, expect, it } from 'vitest';

import {
  EQUITY_DATASET_FAMILIES,
  EQUITY_TRADING_STATUSES,
  equityDataStatusResponseSchema,
  equityEventResponseSchema,
  equitySearchResponseSchema,
  internalEquityDataStatusRequestSchema,
  internalEquityEventRequestSchema,
  internalEquitySearchRequestSchema,
  internalEquitySearchResponseSchema,
} from '../contracts/equity-workspace.contract.js';

const DATA_VERSION = '00000000-0000-4000-8000-000000000001';

/** 覆盖股票中心内部与公开严格合同。 */
describe('equity workspace contracts', () => {
  /** BASE publication 即使部分组件缺失也应保留 AVAILABLE 与 PARTIAL。 */
  it('accepts an available partial discovery publication', () => {
    const result = internalEquitySearchResponseSchema.parse(internalSearchResponse());

    expect(result.release?.completeness).toBe('PARTIAL');
    expect(result.records[0]?.statuses.lifecycleStatus).toBe('LISTED');
  });

  /** 同步服务即使误报研究态资金流能力，严格响应合同也必须失败关闭。 */
  it('rejects unavailable money-flow fields from discovery capabilities', () => {
    const response = internalSearchResponse();

    expect(
      internalEquitySearchResponseSchema.safeParse({
        ...response,
        capabilities: {
          ...response.capabilities,
          sortFields: ['symbol', 'moneyFlowNetAmount'],
          columns: ['symbol', 'listingStatus', 'moneyFlowNetRatio'],
        },
      }).success,
    ).toBe(false);
  });

  /** 停复牌清单未报告证券时必须保留 UNKNOWN，不能伪造成正常成交。 */
  it('accepts unknown trading status with an explicit reason', () => {
    const base = internalSearchResponse();
    const record = base.records[0];
    if (record === undefined) throw new TypeError('Expected discovery record');
    const response = {
      ...base,
      records: [
        {
          ...record,
          statuses: {
            ...record.statuses,
            tradingStatus: 'UNKNOWN' as const,
            tradingStatusReason: 'NO_REPORTED_SUSPENSION',
          },
        },
      ],
    };

    const parsed = internalEquitySearchResponseSchema.parse(response);

    expect(parsed.records[0]?.statuses).toMatchObject({
      tradingStatus: 'UNKNOWN',
      tradingStatusReason: 'NO_REPORTED_SUSPENSION',
    });
  });

  /** 内部搜索请求必须能同时表达全部冻结交易状态，避免新增保守状态后无法筛选。 */
  it('accepts every frozen trading status in one internal search request', () => {
    const parsed = internalEquitySearchRequestSchema.parse({
      tradingStatuses: [...EQUITY_TRADING_STATUSES],
      limit: 50,
    });

    expect(parsed.tradingStatuses).toEqual(EQUITY_TRADING_STATUSES);
  });

  /** 公开响应必须使用 listingStatus 且保留 completeness。 */
  it('accepts public projection and rejects internal lifecycle fields', () => {
    const internal = internalSearchResponse();
    const record = internal.records[0];
    const publicResponse = {
      ...internal,
      release:
        internal.release === null
          ? null
          : {
              dataVersion: internal.release.dataVersion,
              publishedAt: internal.release.publishedAt,
              effectiveAsOf: internal.release.effectiveAsOf,
              knowledgeCutoff: internal.release.knowledgeCutoff,
              qualityStatus: internal.release.qualityStatus,
              completeness: internal.release.completeness,
            },
      records:
        record === undefined
          ? []
          : [
              {
                ...record,
                statuses: {
                  listingStatus: record.statuses.lifecycleStatus,
                  tradingStatus: record.statuses.tradingStatus,
                },
              },
            ],
    };

    expect(equitySearchResponseSchema.parse(publicResponse).release?.completeness).toBe('PARTIAL');
    expect(
      equitySearchResponseSchema.safeParse({
        ...publicResponse,
        records: internal.records,
      }).success,
    ).toBe(false);
  });

  /** 数据状态单次请求必须允许冻结的十八个详情族。 */
  it('accepts every frozen data-status family in one request', () => {
    const parsed = internalEquityDataStatusRequestSchema.parse({
      families: [...EQUITY_DATASET_FAMILIES],
    });

    expect(parsed.families).toHaveLength(EQUITY_DATASET_FAMILIES.length);
  });

  /** 公开 data-status 必须同时锁定身份日期并完整容纳十八个独立数据集状态。 */
  it('accepts an identity-bound eighteen-family data-status envelope', () => {
    const response = {
      identity: {
        exchange: 'SSE',
        symbol: '600519',
        name: '贵州茅台',
        identityAsOf: '2026-07-29',
      },
      datasets: EQUITY_DATASET_FAMILIES.map((family) => ({
        family,
        dataset: `equity.${family.toLowerCase()}`,
        availability: 'UNAVAILABLE',
        freshness: 'UNKNOWN',
        dataVersion: null,
        publishedAt: null,
        effectiveAsOf: null,
        knowledgeCutoff: null,
        sourceLabel: null,
        methodology: null,
        reasonCode: 'NO_PUBLICATION',
        retryable: false,
      })),
    };

    const parsed = equityDataStatusResponseSchema.parse(response);

    expect(parsed.identity?.identityAsOf).toBe('2026-07-29');
    expect(parsed.datasets).toHaveLength(EQUITY_DATASET_FAMILIES.length);
    expect(
      equityDataStatusResponseSchema.safeParse({
        ...response,
        identity: { exchange: 'SSE', symbol: '600519', name: '贵州茅台' },
      }).success,
    ).toBe(false);
  });

  /** 事件请求允许用独立业务日期锚定代码复用下的唯一证券身份。 */
  it('accepts an independent identity as-of anchor for events', () => {
    const parsed = internalEquityEventRequestSchema.parse({
      families: ['DRAGON_TIGER'],
      asOf: '2026-07-29',
      start: '2026-01-01',
      end: '2026-07-29',
      limit: 50,
    });

    expect(parsed.asOf).toBe('2026-07-29');
  });

  /** 公开事件只接受 eventRef 和结构化 facts，不允许内部 eventId 漂出。 */
  it('rejects internal event identifiers from public responses', () => {
    const response = {
      availability: 'AVAILABLE',
      reasonCode: null,
      release: {
        dataVersion: DATA_VERSION,
        publishedAt: '2026-07-30T08:00:00Z',
        qualityStatus: 'passed',
      },
      events: [
        {
          eventRef: `evt_${'a'.repeat(43)}`,
          family: 'EARNINGS_FORECAST',
          kind: 'FORECAST',
          dataVersion: DATA_VERSION,
          facts: [{ code: 'NET_PROFIT_CHANGE', valueLow: '-10.5', valueHigh: '20' }],
        },
      ],
      page: { nextCursor: null, limit: 50 },
    };

    expect(equityEventResponseSchema.parse(response).events).toHaveLength(1);
    expect(
      equityEventResponseSchema.safeParse({
        ...response,
        events: [{ ...response.events[0], eventId: DATA_VERSION }],
      }).success,
    ).toBe(false);
  });
});

/** 构造一条最小但真实语义完整的内部 discovery 响应。 */
function internalSearchResponse() {
  return {
    availability: 'AVAILABLE' as const,
    reasonCode: null,
    release: {
      dataset: 'equity.discovery.eod',
      dataVersion: DATA_VERSION,
      publishedAt: '2026-07-30T08:00:00Z',
      effectiveAsOf: '2026-07-29',
      knowledgeCutoff: '2026-07-30T07:30:00Z',
      qualityStatus: 'warning' as const,
      completeness: 'PARTIAL' as const,
    },
    components: [
      {
        family: 'market',
        dataVersion: DATA_VERSION,
        availability: 'AVAILABLE' as const,
      },
    ],
    capabilities: {
      sortFields: ['symbol'] as const,
      columns: ['symbol', 'listingStatus'] as const,
      maxLimit: 100,
    },
    records: [
      {
        identity: {
          exchange: 'SSE' as const,
          symbol: '600519',
          name: '贵州茅台',
          identityAsOf: '2026-07-29',
        },
        statuses: {
          lifecycleStatus: 'LISTED' as const,
          tradingStatus: 'TRADED' as const,
        },
        market: { tradeDate: '2026-07-29', close: '1418.88', currency: 'CNY' as const },
        capitalization: {
          totalShares: '1256197800',
          totalMarketCapCny: '1782314486324',
          currency: 'CNY' as const,
          methodology: { code: 'close-times-effective-shares', version: 'v1' },
        },
        valuation: { tradeDate: '2026-07-29', peTtm: '22.50' },
        moneyFlow: { tradeDate: '2026-07-29', netAmountCny: '-120000000' },
        memberships: [],
      },
    ],
    page: { nextCursor: null, limit: 50 },
  };
}
