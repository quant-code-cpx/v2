import { describe, expect, it } from 'vitest';

import {
  marketDataQueryRequestSchema,
  parseMarketDataQueryResponse,
  type MarketDataQueryRequest,
} from '../contracts/market-data-access.contract.js';

const REQUEST_ID = 'market/data:request-101';
const ENTITY_REF = '00000000-0000-4000-8000-000000000102';
const DATA_VERSION = '00000000-0000-4000-8000-000000000103';

/** 表示测试中允许定向制造合同漂移的 ETF record。 */
type MutableEtfRecord = Record<string, unknown> & {
  dataVersion: string;
  values: Record<string, unknown>;
};

/** 表示测试中允许切换完整度和 warning 的市场数据响应。 */
type MutableMarketDataResponse = Record<string, unknown> & {
  meta: {
    release: Record<string, unknown>;
    visibility: Record<string, unknown>;
    page: {
      limit: number;
      hasMore: boolean;
      nextCursor: string | null;
    };
    warnings: string[];
  } & Record<string, unknown>;
  records: Array<Record<string, unknown>>;
};

/** 覆盖 ETF v2 请求白名单和标准 typed-record 外壳。 */
describe('ETF v2 market-data contract', () => {
  /** profile 必须显式限定单一交易所，并允许真实的名称包含和代码前缀过滤。 */
  it('accepts the reviewed profile filters and rejects unsafe scope or page drift', () => {
    expect(marketDataQueryRequestSchema.safeParse(profileRequest()).success).toBe(true);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...profileRequest(),
        identity: { entityRefs: [ENTITY_REF] },
      }).success,
    ).toBe(false);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...profileRequest(),
        filters: [{ field: 'exchange', operator: 'IN', values: ['SSE', 'SZSE'] }],
      }).success,
    ).toBe(false);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...profileRequest(),
        page: { limit: 51 },
      }).success,
    ).toBe(false);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...profileRequest(),
        dataset: { code: 'fund.etf.profile.reported', schemaVersion: 3 },
      }).success,
    ).toBe(false);
  });

  /** 四个一期 dataset 必须与 data-sync 共用同一时间、过滤和分页硬上限。 */
  it('freezes the ETF v2 request limits used by both services', () => {
    expect(marketDataQueryRequestSchema.safeParse(navRequest()).success).toBe(true);
    expect(marketDataQueryRequestSchema.safeParse(stateRequest()).success).toBe(true);

    const invalidRequests = [
      {
        ...profileRequest(),
        time: { dimension: 'EFFECTIVE_AT', from: '2026-07-29', to: '2026-07-30' },
      },
      {
        ...barRequest(),
        time: { dimension: 'TRADE_DATE', from: '2025-07-29', to: '2026-07-30' },
      },
      {
        ...navRequest(),
        page: { limit: 367 },
      },
      {
        ...stateRequest(),
        time: { dimension: 'EFFECTIVE_AT', from: '2025-07-29', to: '2026-07-30' },
      },
      {
        ...stateRequest(),
        page: { limit: 501 },
      },
      {
        ...navRequest(),
        filters: [{ field: 'etfEntityRef', operator: 'EQ', values: [ENTITY_REF] }],
      },
      {
        ...stateRequest(),
        filters: [{ field: 'stateDimension', operator: 'EQ', values: ['TRADING'] }],
      },
      {
        ...barRequest(),
        sort: [{ field: 'displayName', direction: 'ASC' }],
      },
      {
        ...barRequest(),
        filters: [
          { field: 'etfEntityRef', operator: 'EQ', values: [ENTITY_REF] },
          { field: 'tradeDate', operator: 'GTE', values: ['2026-07-29'] },
        ],
      },
      {
        ...navRequest(),
        sort: [{ field: 'etfEntityRef', direction: 'ASC' }],
      },
      {
        ...profileRequest(),
        filters: [
          { field: 'exchange', operator: 'EQ', values: ['SSE'] },
          { field: 'listingStatus', operator: 'IN', values: ['LISTED', 'LISTED'] },
        ],
      },
    ];
    for (const request of invalidRequests) {
      expect(marketDataQueryRequestSchema.safeParse(request).success).toBe(false);
    }
  });

  /** PREFIX/CONTAINS 只能用于 profile v2，bar 详情必须精确到单一 ETF UUID。 */
  it('rejects profile-only operators and missing entity scope on daily bars', () => {
    expect(marketDataQueryRequestSchema.safeParse(barRequest()).success).toBe(true);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...barRequest(),
        filters: [{ field: 'tradeDate', operator: 'PREFIX', values: ['2026'] }],
      }).success,
    ).toBe(false);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...barRequest(),
        filters: [],
      }).success,
    ).toBe(false);
  });

  /** ETF v2 只公开真实实现的 CURRENT 与精确 dataVersion，拒绝 PIT、知识版本和方法学旁路。 */
  it('fails closed on unsupported ETF v2 visibility and publication selectors', () => {
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...profileRequest(),
        selection: { qualityStatuses: ['PASSED'], dataVersion: DATA_VERSION },
      }).success,
    ).toBe(true);
    for (const mode of ['PUBLIC_PIT', 'OPERATIONAL_REPLAY']) {
      expect(
        marketDataQueryRequestSchema.safeParse({
          ...profileRequest(),
          visibility: {
            mode,
            asOf: '2026-07-30T09:00:00Z',
            knownAt: '2026-07-30T09:00:00Z',
          },
        }).success,
      ).toBe(false);
    }
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...profileRequest(),
        selection: {
          qualityStatuses: ['PASSED'],
          knownDataVersion: DATA_VERSION,
        },
      }).success,
    ).toBe(false);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...profileRequest(),
        selection: {
          qualityStatuses: ['PASSED'],
          methodology: {
            code: 'etf-profile-reported',
            version: '1',
            kind: 'REPORTED',
          },
        },
      }).success,
    ).toBe(false);
  });

  /** ETF v1 与非 ETF 请求继续使用既有通用合同，不被 v2 字段白名单误伤。 */
  it('keeps ETF v1 and non-ETF requests backward compatible', () => {
    const legacy = {
      ...profileRequest(),
      dataset: { code: 'fund.etf.profile.reported', schemaVersion: 1 },
      businessScope: 'FUND',
      identity: { entityRefs: [ENTITY_REF] },
      fields: ['legacyField'],
      filters: [],
      page: undefined,
    };
    expect(marketDataQueryRequestSchema.safeParse(legacy).success).toBe(true);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...nonEtfRequest(),
        identity: { entityRefs: [ENTITY_REF] },
      }).success,
    ).toBe(true);
    const advancedSelection = {
      qualityStatuses: ['PASSED'],
      knownDataVersion: DATA_VERSION,
      methodology: { code: 'legacy-methodology', version: '1' },
    };
    const pitVisibility = {
      mode: 'PUBLIC_PIT',
      asOf: '2026-07-30T09:00:00Z',
      knownAt: '2026-07-30T09:00:00Z',
    };
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...legacy,
        visibility: pitVisibility,
        selection: advancedSelection,
      }).success,
    ).toBe(true);
    expect(
      marketDataQueryRequestSchema.safeParse({
        ...nonEtfRequest(),
        visibility: pitVisibility,
        selection: advancedSelection,
      }).success,
    ).toBe(true);
  });

  /** AVAILABLE 页必须保留 typed-record 外壳、精确 values 投影、来源与独立 publication 状态。 */
  it('parses a strict bar record and preserves partial or lag metadata', () => {
    const request = marketDataQueryRequestSchema.parse(barRequest());
    const response = availableResponse(request, [barRecord()]);
    response.meta.release.completeness = 'PARTIAL';
    response.meta.warnings = ['source_lag'];

    const parsed = parseMarketDataQueryResponse(response, request);

    expect(parsed.meta.release).toMatchObject({ completeness: 'PARTIAL' });
    expect(parsed.meta.warnings).toEqual(['source_lag']);
    expect(parsed.records[0]).toMatchObject({
      recordType: 'ETF',
      values: { close: '3.945', adjustment: 'UNADJUSTED' },
    });
  });

  /** 扁平化、未知业务字段和记录版本漂移都必须失败关闭。 */
  it('rejects flattened, over-posted, or cross-release ETF records', () => {
    const request = marketDataQueryRequestSchema.parse(barRequest());
    const flattened = { ...barRecord().values };
    expect(() =>
      parseMarketDataQueryResponse(availableResponse(request, [flattened]), request),
    ).toThrow();

    const overPosted = barRecord();
    overPosted.values = { ...overPosted.values, premiumRate: '0.01' };
    expect(() =>
      parseMarketDataQueryResponse(availableResponse(request, [overPosted]), request),
    ).toThrow();

    const wrongVersion = barRecord();
    wrongVersion.dataVersion = '00000000-0000-4000-8000-000000000199';
    expect(() =>
      parseMarketDataQueryResponse(availableResponse(request, [wrongVersion]), request),
    ).toThrow();
  });

  /** 无 publication 是带真实原因的成功空页，不得伪造 dataVersion 或游标。 */
  it('preserves a source-unavailable publication state as an empty success', () => {
    const request = marketDataQueryRequestSchema.parse(barRequest());
    const parsed = parseMarketDataQueryResponse(unavailableResponse(request), request);

    expect(parsed.meta.availability).toBe('SOURCE_UNAVAILABLE');
    expect(parsed.meta.release).toMatchObject({
      state: 'SOURCE_UNAVAILABLE',
      reasonCode: 'PUBLICATION_NOT_AVAILABLE',
    });
    expect(parsed.records).toEqual([]);
  });

  /** 货币市场 ETF 的收益口径未冻结时保留明确不支持状态，不能冒充单位 NAV。 */
  it('preserves a currently-unsupported NAV semantics state as an empty success', () => {
    const request = marketDataQueryRequestSchema.parse(navRequest());
    const parsed = parseMarketDataQueryResponse(unsupportedNavResponse(request), request);

    expect(parsed.meta.availability).toBe('CURRENTLY_UNSUPPORTED');
    expect(parsed.meta.release).toMatchObject({
      state: 'CURRENTLY_UNSUPPORTED',
      reasonCode: 'NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET',
    });
    expect(parsed.meta.coverage).toMatchObject({ pitCoverage: 'UNKNOWN' });
    expect(parsed.records).toEqual([]);
  });

  /** 非数据结果必须同时绑定 availability、稳定 reason 和 NAV dataset，拒绝任意下游说明越界。 */
  it('rejects unavailable reasons that do not match ETF state or dataset', () => {
    const nav = marketDataQueryRequestSchema.parse(navRequest());
    const profile = marketDataQueryRequestSchema.parse(profileRequest());

    expect(
      parseMarketDataQueryResponse(emptyResponse(nav, 'EMPTY', 'NO_MATCHING_FACTS'), nav).meta
        .availability,
    ).toBe('EMPTY');
    expect(() =>
      parseMarketDataQueryResponse(emptyResponse(nav, 'EMPTY', 'PROVIDER_UNAVAILABLE'), nav),
    ).toThrow();
    expect(() =>
      parseMarketDataQueryResponse(
        emptyResponse(nav, 'SOURCE_UNAVAILABLE', 'NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET'),
        nav,
      ),
    ).toThrow();
    expect(() =>
      parseMarketDataQueryResponse(
        emptyResponse(nav, 'CURRENTLY_UNSUPPORTED', 'PUBLICATION_NOT_AVAILABLE'),
        nav,
      ),
    ).toThrow();
    expect(() => parseMarketDataQueryResponse(unsupportedNavResponse(profile), profile)).toThrow();
    expect(() =>
      parseMarketDataQueryResponse(
        emptyResponse(nav, 'SOURCE_UNAVAILABLE', 'UNREVIEWED_DOWNSTREAM_REASON'),
        nav,
      ),
    ).toThrow();
  });
});

/** 构造 ETF profile v2 列表请求。 */
function profileRequest(): Record<string, unknown> {
  return {
    dataset: { code: 'fund.etf.profile.reported', schemaVersion: 2 },
    businessScope: 'ETF',
    time: { dimension: 'EFFECTIVE_AT', from: '2026-07-30', to: '2026-07-30' },
    visibility: { mode: 'CURRENT' },
    selection: { qualityStatuses: ['PASSED', 'WARNED'] },
    fields: ['etfEntityRef', 'exchange', 'symbol', 'displayName', 'listingStatus'],
    filters: [
      { field: 'exchange', operator: 'EQ', values: ['SSE'] },
      { field: 'symbol', operator: 'PREFIX', values: ['51'] },
      { field: 'displayName', operator: 'CONTAINS', values: ['ETF'] },
    ],
    sort: [{ field: 'symbol', direction: 'ASC' }],
    page: { limit: 50 },
  };
}

/** 构造 ETF 未复权日线 v2 详情请求。 */
function barRequest(): Record<string, unknown> {
  return {
    dataset: { code: 'fund.etf.bar.1d.reported', schemaVersion: 2 },
    businessScope: 'ETF',
    time: { dimension: 'TRADE_DATE', from: '2026-07-29', to: '2026-07-30' },
    visibility: { mode: 'CURRENT' },
    selection: { qualityStatuses: ['PASSED', 'WARNED'] },
    fields: [
      'tradeDate',
      'etfEntityRef',
      'open',
      'high',
      'low',
      'close',
      'volume',
      'volumeUnit',
      'amount',
      'currency',
      'tradeStatus',
      'adjustment',
    ],
    filters: [{ field: 'etfEntityRef', operator: 'EQ', values: [ENTITY_REF] }],
    sort: [{ field: 'tradeDate', direction: 'ASC' }],
    page: { limit: 366 },
  };
}

/** 构造 ETF 单位 NAV v2 详情请求。 */
function navRequest(): Record<string, unknown> {
  return {
    dataset: { code: 'fund.etf.nav.1d.reported', schemaVersion: 2 },
    businessScope: 'ETF',
    time: { dimension: 'TRADE_DATE', from: '2026-07-29', to: '2026-07-30' },
    visibility: { mode: 'CURRENT' },
    selection: { qualityStatuses: ['PASSED', 'WARNED'] },
    fields: ['navDate', 'etfEntityRef', 'navKind', 'nav', 'currency', 'finality'],
    filters: [
      { field: 'etfEntityRef', operator: 'EQ', values: [ENTITY_REF] },
      { field: 'navKind', operator: 'EQ', values: ['UNIT'] },
    ],
    sort: [{ field: 'navDate', direction: 'DESC' }],
    page: { limit: 366 },
  };
}

/** 构造 ETF 三维状态 v2 详情请求。 */
function stateRequest(): Record<string, unknown> {
  return {
    dataset: { code: 'fund.etf.trading_state.reported', schemaVersion: 2 },
    businessScope: 'ETF',
    time: { dimension: 'EFFECTIVE_AT', from: '2025-07-30', to: '2026-07-30' },
    visibility: { mode: 'CURRENT' },
    selection: { qualityStatuses: ['PASSED', 'WARNED'] },
    fields: ['etfEntityRef', 'stateDimension', 'state', 'effectiveFrom', 'effectiveTo', 'reason'],
    filters: [{ field: 'etfEntityRef', operator: 'EQ', values: [ENTITY_REF] }],
    sort: [{ field: 'effectiveFrom', direction: 'DESC' }],
    page: { limit: 500 },
  };
}

/** 构造原有非 ETF typed query，验证 v2 规则不扩大影响范围。 */
function nonEtfRequest(): Record<string, unknown> {
  return {
    dataset: { code: 'derivative.bar.1d.reported', schemaVersion: 1 },
    businessScope: 'CONTRACT',
    time: { dimension: 'TRADE_DATE', from: '2026-07-28', to: '2026-07-29' },
    visibility: { mode: 'CURRENT' },
    selection: { qualityStatuses: ['PASSED'] },
    fields: ['tradeDate'],
    filters: [{ field: 'contractEntityRef', operator: 'EQ', values: [ENTITY_REF] }],
    sort: [{ field: 'tradeDate', direction: 'ASC' }],
  };
}

/** 构造真实 typed-record 外壳内的 ETF 未复权日线。 */
function barRecord(): MutableEtfRecord {
  return {
    recordRef: `etf-bar:${ENTITY_REF}:2026-07-30:1`,
    recordType: 'ETF',
    entity: {
      entityRef: ENTITY_REF,
      entityType: 'ETF_LISTING',
      identifiers: [{ scheme: 'venue_symbol', value: 'SSE.510300' }],
    },
    time: { tradeDate: '2026-07-30' },
    publicUsableAt: '2026-07-30T09:00:00Z',
    availabilityBasis: 'OBSERVED_ONLY',
    sourcePublishedAt: null,
    observedAt: '2026-07-30T09:00:00Z',
    dataVersion: DATA_VERSION,
    sourceRef: 'src_etf',
    methodologyVersion: '1',
    qualityStatus: 'PASSED',
    revision: { revisionNumber: 1, currentInPublication: true },
    values: {
      tradeDate: '2026-07-30',
      etfEntityRef: ENTITY_REF,
      open: '3.900',
      high: '3.960',
      low: '3.890',
      close: '3.945',
      volume: '123456',
      volumeUnit: 'LOT',
      amount: '487000000',
      currency: 'CNY',
      tradeStatus: null,
      adjustment: 'UNADJUSTED',
    },
  };
}

/** 构造带 publication、来源、质量和 coverage 的 AVAILABLE 响应。 */
function availableResponse(
  request: MarketDataQueryRequest,
  records: Array<Record<string, unknown>>,
): MutableMarketDataResponse {
  return {
    meta: {
      requestId: REQUEST_ID,
      contractVersion: '1.0.0',
      dataset: request.dataset,
      availability: 'AVAILABLE',
      release: {
        dataVersion: DATA_VERSION,
        publishedAt: '2026-07-30T09:00:00Z',
        knowledgeCutoff: '2026-07-30T08:59:00Z',
        publicUsableAt: '2026-07-30T09:00:00Z',
        effectiveFrom: null,
        effectiveTo: null,
        methodology: { code: 'etf-unadjusted-daily-bar', version: '1', kind: 'REPORTED' },
        sources: [
          {
            sourceRef: 'src_etf',
            publisher: '已批准 ETF 来源',
            sourceDataset: 'ETF 未复权日线',
            authoritative: true,
            redistribution: 'INTERNAL_ONLY',
            coverageNote: null,
          },
        ],
        quality: { status: 'PASSED', issueCodes: [] },
        completeness: 'COMPLETE',
        disclaimers: [],
      },
      visibility: request.visibility,
      page: {
        limit: request.page?.limit ?? 100,
        hasMore: false,
        nextCursor: null,
      },
      coverage: { from: '2026-07-29', to: '2026-07-30', pitCoverage: 'COMPLETE', gaps: [] },
      warnings: [],
      disclaimers: [],
    },
    records,
  };
}

/** 构造无 publication 的成功空响应。 */
function unavailableResponse(request: MarketDataQueryRequest): Record<string, unknown> {
  return emptyResponse(request, 'SOURCE_UNAVAILABLE', 'PUBLICATION_NOT_AVAILABLE');
}

/** 构造 availability、state 与 reason 显式绑定的 ETF 非数据响应。 */
function emptyResponse(
  request: MarketDataQueryRequest,
  availability: 'EMPTY' | 'SOURCE_UNAVAILABLE' | 'CURRENTLY_UNSUPPORTED',
  reasonCode: string,
): Record<string, unknown> {
  return {
    meta: {
      requestId: REQUEST_ID,
      contractVersion: '1.0.0',
      dataset: request.dataset,
      availability,
      release: {
        state: availability,
        observedAt: null,
        reasonCode,
      },
      visibility: request.visibility,
      page: { limit: request.page?.limit ?? 100, hasMore: false, nextCursor: null },
      coverage: { from: null, to: null, pitCoverage: 'UNKNOWN', gaps: [] },
      warnings: ['publication_unavailable'],
      disclaimers: [],
    },
    records: [],
  };
}

/** 构造货币市场 ETF NAV 口径当前不支持的成功空响应。 */
function unsupportedNavResponse(request: MarketDataQueryRequest): Record<string, unknown> {
  const response = emptyResponse(
    request,
    'CURRENTLY_UNSUPPORTED',
    'NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET',
  );
  const meta = response.meta as Record<string, unknown>;
  const release = meta.release as Record<string, unknown>;
  release.observedAt = '2026-07-30T09:00:00Z';
  meta.coverage = { from: null, to: null, pitCoverage: 'UNKNOWN', gaps: [] };
  meta.warnings = ['nav_semantics_unsupported_money_market'];
  return response;
}
