import { describe, expect, it } from 'vitest';

import {
  marketCalendarPageSchema,
  marketEquityMoneyFlowRankingPageSchema,
  marketIndexBarPageSchema,
  marketOverviewSchema,
  marketSectorStrengthPageSchema,
  swIndustryBarPageSchema,
  swIndustryConstituentPageSchema,
  swIndustryValuationSchema,
  type MarketEquityMoneyFlowRankingPage,
  type SwIndustryConstituentPage,
} from '../contracts/market-overview.contract.js';
import {
  createMarketIndexBarPageFixture,
  createMarketInputVersions,
  createMarketOverviewFixture,
} from './market-overview.fixtures.js';

describe('market overview contracts', () => {
  /** 验证完整包的四指数、质量与十进制字符串合同可以通过。 */
  it('accepts a complete atomic overview bundle', () => {
    expect(marketOverviewSchema.parse(createMarketOverviewFixture())).toBeDefined();
  });

  /** 验证市场宽度存在未知证券时不会被 API 当作可信完整包。 */
  it('rejects overview breadth with unknown equities', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      breadth: { ...fixture.breadth, unknown: 1 },
    });
    expect(result.success).toBe(false);
  });

  /** 验证质量通过数不足时拒绝半完整 bundle。 */
  it('rejects a bundle whose required components did not all pass', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      quality: { ...fixture.quality, passedCount: 1 },
    });
    expect(result.success).toBe(false);
  });

  /** 验证 freshness 不会在存在交易日滞后时继续宣称 current。 */
  it('rejects current freshness when the complete bundle lags an eligible trading day', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      status: {
        ...fixture.status,
        latestEligibleTradeDate: '2026-07-30',
        latestAttemptedTradeDate: '2026-07-30',
        lagTradingDays: 1,
        freshnessReason: 'latest_eligible_bundle_incomplete',
      },
    });
    expect(result.success).toBe(false);
  });

  /** 验证同日回滚可以保持零交易日滞后，但必须显式标为 stale。 */
  it('accepts a same-day publication rollback as stale with zero lag', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      status: {
        ...fixture.status,
        freshness: 'stale',
        freshnessReason: 'publication_rollback',
      },
    });
    expect(result.success).toBe(true);
  });

  /** 验证精确历史读取使用冻结收盘状态，而不是用当前时钟重算旧交易日。 */
  it('accepts a closed frozen historical snapshot status', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      status: {
        ...fixture.status,
        marketState: 'closed',
        marketStateAsOf: '2026-07-30T15:00:00+08:00',
        freshness: 'stale',
        latestEligibleTradeDate: fixture.tradeDate,
        latestAttemptedTradeDate: null,
        lagTradingDays: 0,
        freshnessReason: 'historical_snapshot',
      },
    });
    expect(result.success).toBe(true);
  });

  /** 验证历史标记不能掩盖与所选交易日不一致的 eligible 日期。 */
  it('rejects a historical status whose eligible date differs from the bundle', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      status: {
        ...fixture.status,
        marketState: 'closed',
        freshness: 'stale',
        latestEligibleTradeDate: '2026-07-29',
        latestAttemptedTradeDate: null,
        lagTradingDays: 0,
        freshnessReason: 'historical_snapshot',
      },
    });
    expect(result.success).toBe(false);
  });

  /** 验证 eligible bundle 不完整时不能用零滞后掩盖首页 publication 过期。 */
  it('rejects an incomplete eligible bundle with zero lag', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      status: {
        ...fixture.status,
        freshness: 'stale',
        freshnessReason: 'latest_eligible_bundle_incomplete',
      },
    });
    expect(result.success).toBe(false);
  });

  /** 验证五日强弱 publication 必须保留五个精确共同交易日输入版本。 */
  it('accepts exact input publications for a complete sector strength window', () => {
    expect(marketSectorStrengthPageSchema.safeParse(sectorStrengthFixture()).success).toBe(true);
  });

  /** 验证输入版本少于窗口时拒绝不可复验的板块强弱结果。 */
  it('rejects a sector strength publication with incomplete input lineage', () => {
    const fixture = sectorStrengthFixture();
    fixture.inputDataVersions.pop();
    expect(marketSectorStrengthPageSchema.safeParse(fixture).success).toBe(false);
  });

  /** 验证同一个输入 publication 不能在完整窗口 lineage 中重复计数。 */
  it('rejects duplicate sector strength input publications', () => {
    const fixture = sectorStrengthFixture();
    const firstVersion = fixture.inputDataVersions[0];
    if (firstVersion === undefined)
      throw new Error('strength input version fixture is unavailable');
    fixture.inputDataVersions[1] = firstVersion;
    expect(marketSectorStrengthPageSchema.safeParse(fixture).success).toBe(false);
  });

  /** 验证指数数量不足时不能用其他指标或空位冒充四个主要指数。 */
  it('rejects a bundle without all four primary indices', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      indices: fixture.indices.slice(0, 3),
    });
    expect(result.success).toBe(false);
  });

  /** 验证四项长度不能用重复指数占位，必须完整覆盖稳定身份集合。 */
  it('rejects duplicate primary index identities', () => {
    const fixture = createMarketOverviewFixture();
    const first = fixture.indices[0];
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      indices: first === undefined ? [] : [first, first, first, first],
    });
    expect(result.success).toBe(false);
  });

  /** 验证指数来源未报告成交量或成交额时保留 null，而不是误判合同漂移。 */
  it('accepts explicit null index volume and amount values', () => {
    const fixture = createMarketOverviewFixture();
    const first = fixture.indices[0];
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      indices:
        first === undefined
          ? []
          : [{ ...first, volume: null, amountCny: null }, ...fixture.indices.slice(1)],
    });
    expect(result.success).toBe(true);
  });

  /** 验证指数日线可在 7500 天查询上限携带完整 lineage，并明确成交量为手。 */
  it('accepts the maximum index lineage with an explicit lot volume unit', () => {
    const result = marketIndexBarPageSchema.safeParse(createMarketIndexBarPageFixture(7_500));
    expect(result.success).toBe(true);
  });

  /** 验证指数 composite lineage 不能重复计入同一输入 publication。 */
  it('rejects duplicate input publications in an index bar composite', () => {
    const fixture = createMarketIndexBarPageFixture();
    const duplicateVersion = fixture.inputDataVersions[0];
    if (duplicateVersion === undefined) throw new Error('input version fixture is unavailable');
    const result = marketIndexBarPageSchema.safeParse({
      ...fixture,
      inputDataVersions: [duplicateVersion, duplicateVersion],
    });
    expect(result.success).toBe(false);
  });

  /** 验证金额必须保持十进制字符串，禁止 JSON number 精度漂移。 */
  it('rejects numeric turnover amounts', () => {
    const fixture = createMarketOverviewFixture();
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      turnover: { ...fixture.turnover, totalAmountCny: 1_100_000_000_000 },
    });
    expect(result.success).toBe(false);
  });

  /** 验证首页流入榜不能包含零值或净流出证券。 */
  it('rejects non-positive net amounts in overview inflow rankings', () => {
    const fixture = createMarketOverviewFixture();
    const first = fixture.equityMoneyFlowRankings.inflow[0];
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      equityMoneyFlowRankings: {
        ...fixture.equityMoneyFlowRankings,
        inflow: first === undefined ? [] : [{ ...first, netAmountCny: '-0.00' }],
      },
    });
    expect(result.success).toBe(false);
  });

  /** 验证首页流出榜不能包含零值或净流入证券。 */
  it('rejects non-negative net amounts in overview outflow rankings', () => {
    const fixture = createMarketOverviewFixture();
    const first = fixture.equityMoneyFlowRankings.outflow[0];
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      equityMoneyFlowRankings: {
        ...fixture.equityMoneyFlowRankings,
        outflow: first === undefined ? [] : [{ ...first, netAmountCny: '1' }],
      },
    });
    expect(result.success).toBe(false);
  });

  /** 验证独立资金流排行的请求方向与每个非零净额符号一致。 */
  it('rejects direction-inconsistent items in an equity money-flow page', () => {
    const fixture = createEquityMoneyFlowPageFixture();
    expect(marketEquityMoneyFlowRankingPageSchema.safeParse(fixture).success).toBe(true);
    expect(
      marketEquityMoneyFlowRankingPageSchema.safeParse({
        ...fixture,
        direction: 'outflow',
      }).success,
    ).toBe(false);
  });

  /** 验证 P0 首页沪深 universe 不会因通用证券身份支持而混入北交所。 */
  it('rejects BSE equities outside the frozen overview universe', () => {
    const fixture = createMarketOverviewFixture();
    const first = fixture.equityRankings.gainers[0];
    const result = marketOverviewSchema.safeParse({
      ...fixture,
      equityRankings: {
        ...fixture.equityRankings,
        gainers: first === undefined ? [] : [{ ...first, exchange: 'BSE' }],
      },
    });
    expect(result.success).toBe(false);
  });

  /** 验证申万日线不能错误标记为周月聚合方法学。 */
  it('requires period-specific methodology for SW bars', () => {
    const result = swIndustryBarPageSchema.safeParse({
      dataVersion: '00000000-0000-4000-8000-000000000002',
      publishedAt: '2026-07-30T16:00:00+08:00',
      industry: { code: '801010.SI', name: '农林牧渔', level: 1, parentCode: null },
      period: '1d',
      volumeUnit: 'provider_native',
      source: createMarketOverviewFixture().indices[0]?.source,
      methodology: {
        id: 'calendar-bounded-ohlcv-aggregation',
        version: '1',
        status: 'platform_derived',
        inputDataset: 'sw.market-data',
        previousClose: {
          kind: 'derived',
          id: 'period-opening-previous-close-from-daily',
          version: '1',
          inputs: ['daily.previousClose'],
        },
      },
      inputDataVersions: ['00000000-0000-4000-8000-000000000006'],
      finality: 'final',
      items: [
        {
          period: '1d',
          periodKey: '1d:2026:211',
          periodStart: '2026-07-30',
          periodEnd: '2026-07-30',
          open: '100',
          high: '102',
          low: '99',
          close: '101',
          change: '1',
          changePercent: '1',
          volume: '1000',
          amountCny: '1000000',
          previousClose: '100',
          amplitudePercent: '3',
          turnoverPercent: null,
          isFinal: true,
        },
      ],
      nextCursor: null,
    });
    expect(result.success).toBe(false);
  });

  /** 验证申万月线携带周期边界、输入版本和聚合方法学，并保留来源空值。 */
  it('accepts a materialized SW monthly bar publication', () => {
    const result = swIndustryBarPageSchema.safeParse({
      dataVersion: '00000000-0000-4000-8000-000000000004',
      publishedAt: '2026-07-30T16:00:00+08:00',
      industry: { code: '801010.SI', name: '农林牧渔', level: 1, parentCode: null },
      period: '1mo',
      volumeUnit: 'provider_native',
      source: createMarketOverviewFixture().indices[0]?.source,
      methodology: {
        id: 'calendar-bounded-ohlcv-aggregation',
        version: '1',
        status: 'platform_derived',
        inputDataset: 'sw.market-data',
        previousClose: {
          kind: 'derived',
          id: 'period-opening-previous-close-from-daily',
          version: '1',
          inputs: ['daily.previousClose'],
        },
      },
      inputDataVersions: createMarketInputVersions(7_500),
      finality: 'final',
      items: [
        {
          period: '1mo',
          periodKey: '1mo:2026:007',
          periodStart: '2026-07-01',
          periodEnd: '2026-07-30',
          open: '100',
          high: '102',
          low: '99',
          close: '101',
          change: '1',
          changePercent: '1',
          volume: null,
          amountCny: null,
          previousClose: '100',
          amplitudePercent: '3',
          turnoverPercent: null,
          isFinal: true,
        },
      ],
      nextCursor: null,
    });
    expect(result.success).toBe(true);
  });

  /** 验证交易日历首行或供应商空前序日使用显式 null。 */
  it('accepts an explicit null previous trading date', () => {
    const result = marketCalendarPageSchema.safeParse({
      dataVersion: '00000000-0000-4000-8000-000000000008',
      publishedAt: '2026-07-30T16:00:00+08:00',
      timezone: 'Asia/Shanghai',
      sessionScheduleVersion: 'cn-a-cash-2026-v1',
      source: createMarketOverviewFixture().indices[0]?.source,
      quality: {
        status: 'passed',
        checks: [
          {
            code: 'calendar-schema',
            status: 'passed',
            actual: 'valid',
            expected: 'valid',
          },
        ],
      },
      items: [
        {
          venue: 'SSE',
          tradeDate: '2026-07-30',
          isTradingDay: true,
          previousTradingDate: null,
          sessions: [],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  /** 验证申万正式成分来源未报告调入或调出日时保持 null。 */
  it('accepts explicit null SW membership boundaries', () => {
    const result = swIndustryConstituentPageSchema.safeParse(
      createSwIndustryConstituentPageFixture(),
    );
    expect(result.success).toBe(true);
  });

  /** 验证申万成员调入日必须严格早于调出日。 */
  it('rejects an inverted SW membership effective interval', () => {
    const fixture = createSwIndustryConstituentPageFixture();
    const result = swIndustryConstituentPageSchema.safeParse({
      ...fixture,
      items: [{ ...fixture.items[0], inDate: '2026-07-30', outDate: '2026-07-30' }],
    });
    expect(result.success).toBe(false);
  });

  /** 验证 active 成员的半开有效区间必须覆盖页面快照日。 */
  it('rejects an SW active membership outside snapshotDate', () => {
    const fixture = createSwIndustryConstituentPageFixture();
    const result = swIndustryConstituentPageSchema.safeParse({
      ...fixture,
      items: [{ ...fixture.items[0], inDate: '2026-07-31', outDate: null }],
    });
    expect(result.success).toBe(false);
  });

  /** 验证来源未报告的 PE_TTM 与股息率保留 null，而 PE/PB 可逐字段直报。 */
  it('accepts explicit unavailable SW valuation metrics', () => {
    const result = swIndustryValuationSchema.safeParse({
      dataVersion: '00000000-0000-4000-8000-000000000003',
      tradeDate: '2026-07-30',
      publishedAt: '2026-07-30T16:00:00+08:00',
      industry: { code: '801010.SI', name: '农林牧渔', level: 1, parentCode: null },
      source: createMarketOverviewFixture().indices[0]?.source,
      methodology: {
        id: 'sw-source-reported-valuation',
        version: '1',
        owner: 'Shenwan',
        status: 'mixed_per_field',
      },
      inputDataVersions: [
        '00000000-0000-4000-8000-000000000022',
        '00000000-0000-4000-8000-000000000023',
      ],
      valuation: {
        pe: {
          value: '18.2',
          availability: 'available',
          methodology: { kind: 'source_reported', sourceField: 'pe' },
        },
        peTtm: { value: null, availability: 'source_not_reported', methodology: null },
        pb: {
          value: '2.1',
          availability: 'available',
          methodology: { kind: 'source_reported', sourceField: 'pb' },
        },
        dividendYield: {
          value: null,
          availability: 'source_not_reported',
          methodology: null,
        },
      },
      finality: 'final',
    });
    expect(result.success).toBe(true);
  });
});

/** 构造一个覆盖五个共同交易日且全量样本通过的板块强弱 publication。 */
function sectorStrengthFixture(): {
  dataVersion: string;
  tradeDate: string;
  publishedAt: string;
  scheme: 'eastmoney.industry';
  window: 5;
  order: 'desc';
  methodologyVersion: string;
  source: NonNullable<ReturnType<typeof createMarketOverviewFixture>['indices'][number]>['source'];
  inputDataVersions: string[];
  quality: {
    status: 'passed';
    validUniverseCount: number;
    checks: Array<{ code: string; status: 'passed'; actual: string; expected: string }>;
  };
  items: Array<{
    rank: number;
    sectorCode: string;
    name: string;
    changePercent: string;
    turnoverPercent: null;
    amountCny: null;
    cumulativeReturn: string;
    upDays: number;
    medianRank: string;
    validSamples: number;
    coverage: '1';
  }>;
  nextCursor: null;
} {
  const source = createMarketOverviewFixture().indices[0]?.source;
  if (source === undefined) throw new Error('market source fixture is unavailable');
  return {
    dataVersion: '00000000-0000-4000-8000-000000000010',
    tradeDate: '2026-07-30',
    publishedAt: '2026-07-30T16:00:00+08:00',
    scheme: 'eastmoney.industry',
    window: 5,
    order: 'desc',
    methodologyVersion: 'sector-strength-v1',
    source,
    inputDataVersions: [
      '00000000-0000-4000-8000-000000000011',
      '00000000-0000-4000-8000-000000000012',
      '00000000-0000-4000-8000-000000000013',
      '00000000-0000-4000-8000-000000000014',
      '00000000-0000-4000-8000-000000000015',
    ],
    quality: {
      status: 'passed',
      validUniverseCount: 1,
      checks: [{ code: 'full-window', status: 'passed', actual: '5', expected: '5' }],
    },
    items: [
      {
        rank: 1,
        sectorCode: 'BK0475',
        name: '证券',
        changePercent: '1.2',
        turnoverPercent: null,
        amountCny: null,
        cumulativeReturn: '5.1',
        upDays: 4,
        medianRank: '2',
        validSamples: 5,
        coverage: '1',
      },
    ],
    nextCursor: null,
  };
}

/** 构造一页方向与净额符号一致的证券资金流 publication。 */
function createEquityMoneyFlowPageFixture(): MarketEquityMoneyFlowRankingPage {
  const overview = createMarketOverviewFixture();
  const item = overview.equityMoneyFlowRankings.inflow[0];
  if (item === undefined) throw new Error('money-flow item fixture is unavailable');
  return {
    dataVersion: '00000000-0000-4000-8000-000000000016',
    tradeDate: overview.tradeDate,
    publishedAt: overview.publishedAt,
    source: overview.equityMoneyFlowRankings.source,
    direction: 'inflow',
    methodology: {
      id: 'tushare-order-size-flow',
      version: '1',
      semanticFamily: 'order_size_flow',
      status: 'source_reported',
    },
    universe: 'CN-A-SSE-SZSE-TRADED',
    coverage: '1',
    items: [item],
    finality: 'final',
    quality: {
      status: 'passed',
      checks: [
        {
          code: 'non-zero-direction',
          status: 'passed',
          actual: 'inflow-positive',
          expected: 'inflow-positive',
        },
      ],
    },
    nextCursor: null,
  };
}

/** 构造一页允许来源缺失调入调出边界的申万正式成分 publication。 */
function createSwIndustryConstituentPageFixture(): SwIndustryConstituentPage {
  const source = createMarketOverviewFixture().indices[0]?.source;
  if (source === undefined) throw new Error('market source fixture is unavailable');
  return {
    dataVersion: '00000000-0000-4000-8000-000000000009',
    snapshotDate: '2026-07-30',
    publishedAt: '2026-07-30T16:00:00+08:00',
    historyMode: 'latest_revision_effective_interval',
    knowledgeCutoff: '2026-07-30T15:59:00+08:00',
    observedAt: '2026-07-30T15:58:00+08:00',
    industry: { code: '801010.SI', name: '农林牧渔', level: 1, parentCode: null },
    source,
    methodology: {
      id: 'quant-v2.sw-membership.v1',
      version: '1',
      status: 'source_reported',
      temporalSemantics: 'latest_revision_effective_interval',
    },
    inputDataVersions: [
      '00000000-0000-4000-8000-000000000020',
      '00000000-0000-4000-8000-000000000021',
    ],
    items: [
      {
        exchange: 'SSE',
        symbol: '600000',
        name: '浦发银行',
        inDate: null,
        outDate: null,
        isActive: true,
      },
    ],
    nextCursor: null,
  };
}
