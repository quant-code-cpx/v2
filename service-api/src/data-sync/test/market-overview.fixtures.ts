import type { MarketIndexBarPage, MarketOverview } from '../contracts/market-overview.contract.js';

const OBSERVED_AT = '2026-07-30T15:30:00+08:00';

/** 构造一个不含私有字段的真实合同形状来源绑定。 */
function source(sourceDataset: string) {
  return {
    provider: 'tushare-pro' as const,
    upstreamSource: 'tushare.pro',
    sourceDataset,
    observedAt: OBSERVED_AT,
    adapterVersion: '1.0.0',
    schemaFingerprint: 'a'.repeat(64),
  };
}

/** 构造指定数量、顺序稳定且互不重复的测试 publication UUID。 */
export function createMarketInputVersions(count: number): string[] {
  const versions: string[] = [];
  for (let index = 1; index <= count; index += 1) {
    versions.push(`00000000-0000-4000-8000-${String(index).padStart(12, '0')}`);
  }
  return versions;
}

/** 构造可扩到 lineage 与分页容量上限的指数日线 publication。 */
export function createMarketIndexBarPageFixture(
  inputVersionCount = 1,
  itemCount = 1,
): MarketIndexBarPage {
  const items: MarketIndexBarPage['items'] = [];
  for (let index = 0; index < itemCount; index += 1) {
    items.push({
      tradeDate: '2026-07-30',
      open: '3485',
      high: '3510',
      low: '3475',
      close: '3500.5',
      previousClose: '3480',
      change: '20.5',
      changePercent: '0.59',
      volume: '300000000',
      amountCny: '450000000000',
      finality: 'final',
    });
  }
  return {
    dataVersion: '00000000-0000-4000-8000-000000000017',
    publishedAt: '2026-07-30T16:00:00+08:00',
    index: { indexId: 'sse-composite', name: '上证指数' },
    period: '1d',
    volumeUnit: 'lot',
    source: source('index_daily'),
    inputDataVersions: createMarketInputVersions(inputVersionCount),
    items,
    nextCursor: null,
  };
}

/** 构造市场合同测试使用的最小完整首页 bundle。 */
export function createMarketOverviewFixture(): MarketOverview {
  const equity = {
    rank: 1,
    exchange: 'SSE' as const,
    symbol: '600000',
    name: '浦发银行',
    close: '10.50',
    changePercent: '1.20',
    amountCny: '1000000000',
    turnoverPercent: '0.50',
  };
  const flowEquity = {
    rank: 1,
    exchange: 'SSE' as const,
    symbol: '600000',
    name: '浦发银行',
    netAmountCny: '10000000',
    buyLargeAmountCny: '20000000',
    sellLargeAmountCny: '10000000',
    changePercent: '1.20',
  };
  const sector = {
    rank: 1,
    sectorCode: 'BK0001',
    name: '银行',
    changePercent: '1.50',
    turnoverPercent: '0.80',
    amountCny: '50000000000',
    leadingEquity: {
      exchange: 'SSE' as const,
      symbol: '600000',
      name: '浦发银行',
      changePercent: '1.20',
    },
    validSamples: 20,
  };
  const index = {
    point: '3500.50',
    previousClose: '3480.00',
    change: '20.50',
    changePercent: '0.59',
    open: '3485.00',
    high: '3510.00',
    low: '3475.00',
    volume: '300000000',
    volumeUnit: 'lot' as const,
    amountCny: '450000000000',
    source: source('index_daily'),
  };

  return {
    dataVersion: '00000000-0000-4000-8000-000000000001',
    tradeDate: '2026-07-30',
    publishedAt: '2026-07-30T16:00:00+08:00',
    finality: 'final',
    status: {
      marketState: 'closed',
      marketStateAsOf: '2026-07-30T16:00:00+08:00',
      marketStateMethodology: 'calendar_schedule_derived',
      freshness: 'current',
      latestEligibleTradeDate: '2026-07-30',
      latestAttemptedTradeDate: '2026-07-30',
      lagTradingDays: 0,
      eodEligibilityScheduleVersion: 'cn-a-eod-eligibility-2026-v1',
      freshnessReason: 'latest_eligible_complete',
      quality: 'passed',
    },
    indices: [
      { indexId: 'sse-composite', name: '上证指数', ...index },
      { indexId: 'szse-component', name: '深证成指', ...index },
      { indexId: 'csi-300', name: '沪深300', ...index },
      { indexId: 'chinext', name: '创业板指', ...index },
    ],
    turnover: {
      label: '沪深 A 股成交额',
      universe: 'CN-A-SSE-SZSE',
      methodologyId: 'sum-tushare-daily-a-share-amount-cny-v1',
      sseAmountCny: '500000000000',
      szseAmountCny: '600000000000',
      totalAmountCny: '1100000000000',
      previousTotalAmountCny: '1000000000000',
      changeAmountCny: '100000000000',
      changePercent: '10.00',
    },
    breadth: {
      eligible: 5_250,
      advancing: 3_000,
      flat: 200,
      declining: 2_000,
      suspended: 50,
      unknown: 0,
    },
    limits: { limitUp: 80, limitDown: 5, rulesVersion: 'cn-a-limits-2026-01' },
    marketMoneyFlow: {
      source: source('moneyflow_mkt_dc'),
      methodologyId: 'eastmoney-market-flow-dc',
      methodologyVersion: 'unknown',
      netAmountCny: '5000000000',
    },
    equityMoneyFlowRankings: {
      source: source('moneyflow'),
      methodologyId: 'tushare-order-size-flow',
      methodologyVersion: '1',
      universe: 'CN-A-SSE-SZSE-TRADED',
      coverage: '1',
      inflow: [flowEquity],
      outflow: [{ ...flowEquity, netAmountCny: '-10000000' }],
    },
    equityRankings: {
      gainers: [equity],
      losers: [{ ...equity, changePercent: '-1.20' }],
      amount: [equity],
      turnover: [equity],
    },
    sectorRankings: {
      eastmoneyIndustry: {
        strongest: [sector],
        weakest: [{ ...sector, changePercent: '-1.50' }],
      },
      eastmoneyConcept: {
        strongest: [{ ...sector, sectorCode: 'BK1001', name: '机器人' }],
        weakest: [
          {
            ...sector,
            sectorCode: 'BK1002',
            name: '低空经济',
            changePercent: '-1.10',
          },
        ],
      },
    },
    attentionSignals: [
      {
        signalId: 'turnover-expanded',
        ruleId: 'turnover-day-over-day',
        rulesVersion: '1',
        severity: 'info',
        title: '沪深 A 股成交额较上一交易日放大',
        evidence: [
          {
            metric: 'turnoverChangePercent',
            currentValue: '10',
            threshold: '8',
            unit: 'percent',
          },
        ],
      },
    ],
    quality: {
      componentCount: 2,
      passedCount: 2,
      universeVersion: 'CN-A-2026-07-30',
      sourceBindings: [
        { role: 'external', component: 'equity.quote.eod', ...source('daily') },
        {
          role: 'derived',
          component: 'market.turnover.eod',
          provider: 'quant-v2-derivation',
          upstreamSource: 'Tushare canonical inputs',
          sourceDataset: 'market.turnover.eod',
          observedAt: OBSERVED_AT,
          adapterVersion: 'market-overview-derivation-1',
          schemaFingerprint: 'b'.repeat(64),
          methodology: {
            id: 'sum-tushare-daily-a-share-amount-cny-v1',
            version: '1',
            status: 'platform_derived',
          },
        },
      ],
      checks: [
        {
          code: 'turnover-universe',
          status: 'passed',
          actual: 'CN-A-SSE-SZSE',
          expected: 'CN-A-SSE-SZSE',
        },
        {
          code: 'turnover-daily-info-reconciliation',
          status: 'passed',
          actual: 'within-tolerance',
          expected: 'within-tolerance',
        },
      ],
    },
  };
}
