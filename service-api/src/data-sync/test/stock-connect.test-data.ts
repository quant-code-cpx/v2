/** 测试合同使用的不可变 bundle 版本，不进入任何运行时响应路径。 */
export const STOCK_CONNECT_TEST_DATA_VERSION = 'stock-connect.2026-07-29.revision-1';

/** 构造严格合同需要的已报告金额事实。 */
export function reportedMoney(
  amount = '100.00',
  currency: 'CNY' | 'HKD' = 'CNY',
): Record<string, unknown> {
  return {
    availability: 'REPORTED',
    value: { amount, currency, unit: 'BASE' },
    lineageRef: 'release:market-stat',
  };
}

/** 构造带输入 release 的平台派生净额事实。 */
export function derivedMoney(
  amount = '10.00',
  currency: 'CNY' | 'HKD' = 'CNY',
): Record<string, unknown> {
  return {
    availability: 'DERIVED',
    value: { amount, currency, unit: 'BASE' },
    lineageRef: 'release:active:buy-minus-sell-v1',
  };
}

/** 构造制度未披露或来源不可用的空金额事实。 */
export function unavailableMoney(
  availability:
    'NOT_DISCLOSED_BY_REGIME' | 'SOURCE_MISSING' | 'NOT_APPLICABLE' = 'NOT_DISCLOSED_BY_REGIME',
): Record<string, unknown> {
  return { availability, value: null, lineageRef: null };
}

/** 构造完整市场统计，保持成交额与净额独立。 */
export function stockConnectMarketStats(): Record<string, unknown> {
  return {
    buyAmount: unavailableMoney(),
    sellAmount: unavailableMoney(),
    turnoverAmount: reportedMoney(),
    netBuyAmount: unavailableMoney(),
    tradeCount: {
      availability: 'REPORTED',
      value: 42,
      lineageRef: 'release:market-stat',
    },
    etfTurnoverAmount: reportedMoney('20.00'),
  };
}

/** 构造人民币额度与状态强绑定的日终通道状态。 */
export function stockConnectChannelStatus(): Record<string, unknown> {
  return {
    tradingDay: true,
    sessionState: 'CLOSED',
    buyOrderAccepted: false,
    sellOrderAccepted: false,
    quotaState: 'ACTUAL_REPORTED',
    quotaBalance: reportedMoney('500.00', 'CNY'),
    observedAt: '2026-07-29T10:00:00Z',
    finality: 'END_OF_DAY',
  };
}

/** 构造带真实来源时间语义的测试 publication。 */
export function stockConnectPublication(): Record<string, unknown> {
  return {
    bundleReleaseId: '00000000-0000-4000-8000-000000000024',
    dataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
    tradeDate: '2026-07-29',
    publishedAt: '2026-07-29T10:20:00Z',
    qualityStatus: 'APPROVED',
    qualityIssues: [],
    sourceRefs: [
      {
        sourceCode: 'HKEX_DATA_MARKETPLACE',
        productName: 'Stock Connect Daily Statistics',
        sourcePublicationAvailability: 'NOT_PROVIDED_BY_SOURCE',
        sourcePublicationAt: null,
        sourceObservedAt: '2026-07-29T10:05:00Z',
        sourceFileSha256: 'a'.repeat(64),
      },
    ],
  };
}

/** 构造由持久化日历与已发布 bundle 共同支持的 readiness 成功响应。 */
export function stockConnectReadinessResponse(): Record<string, unknown> {
  const body = {
    schemaVersion: 'quant-v2.stock-connect-readiness.v1',
    mode: 'LATEST',
    selectedChannels: ['SH_NORTHBOUND'],
    requestedExactDate: null,
    candidateTradeDate: '2026-07-29',
    readyTradeDate: '2026-07-29',
    observedAt: '2026-07-29T10:20:00Z',
    calendar: {
      dataVersion: 'b'.repeat(64),
      observedAt: '2026-07-29T08:00:00Z',
      sourceFileSha256: 'c'.repeat(64),
      sourcePublicationAt: null,
      publicationAvailability: 'NOT_REPORTED',
    },
    channels: [
      {
        channel: 'SH_NORTHBOUND',
        calendarState: 'OPEN',
        state: 'READY',
        reasonCode: 'BUNDLE_PUBLISHED',
        bundleDataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
        evidenceObservedAt: '2026-07-29T10:20:00Z',
      },
    ],
  };
  return {
    ...body,
    dataVersion: createHash('sha256').update(canonicalJson(body), 'utf8').digest('hex'),
  };
}

/** 返回与 Python 同步服务共享的 Unicode、null 与数组规范哈希固定向量。 */
export function stockConnectReadinessCrossLanguageVector(): Record<string, unknown> {
  return {
    schemaVersion: 'quant-v2.stock-connect-readiness.v1',
    dataVersion: 'abe5d1926e56f9f60959b27141e450ad1a0f580437e59a8e737a1efe34276307',
    mode: 'EXACT',
    selectedChannels: ['SH_NORTHBOUND', 'SZ_SOUTHBOUND'],
    requestedExactDate: '2026-07-30',
    candidateTradeDate: '2026-07-30',
    readyTradeDate: null,
    observedAt: '2026-07-30T10:00:00Z',
    calendar: {
      dataVersion: 'a'.repeat(64),
      observedAt: null,
      sourceFileSha256: null,
      sourcePublicationAt: null,
      publicationAvailability: 'SOURCE_MISSING',
    },
    channels: [
      {
        channel: 'SH_NORTHBOUND',
        calendarState: 'OPEN',
        state: 'READY',
        reasonCode: 'BUNDLE_PUBLISHED',
        bundleDataVersion: '版本-α',
        evidenceObservedAt: '2026-07-30T10:00:00Z',
      },
      {
        channel: 'SZ_SOUTHBOUND',
        calendarState: 'UNKNOWN',
        state: 'SOURCE_MISSING',
        reasonCode: 'CALENDAR_SOURCE_MISSING',
        bundleDataVersion: null,
        evidenceObservedAt: '2026-07-30T10:00:00Z',
      },
    ],
  };
}

/** 构造一条沪股通当前日通道汇总。 */
export function stockConnectChannelSummary(): Record<string, unknown> {
  return {
    channel: 'SH_NORTHBOUND',
    direction: 'NORTHBOUND',
    route: 'SHANGHAI',
    tradeDate: '2026-07-29',
    stats: stockConnectMarketStats(),
    status: stockConnectChannelStatus(),
    activeSecurityCount: 1,
  };
}

/** 构造严格的互联互通总览成功响应。 */
export function stockConnectOverviewResponse(): Record<string, unknown> {
  return {
    resolvedTradeDate: '2026-07-29',
    dateResolution: 'LATEST_COMMON',
    channels: [stockConnectChannelSummary()],
    trend: [
      {
        channel: 'SH_NORTHBOUND',
        tradeDate: '2026-07-29',
        dataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
        stats: stockConnectMarketStats(),
        status: stockConnectChannelStatus(),
      },
    ],
    publication: stockConnectPublication(),
  };
}

/** 构造严格的单通道成功响应。 */
export function stockConnectChannelResponse(): Record<string, unknown> {
  return {
    resolvedTradeDate: '2026-07-29',
    dateResolution: 'LATEST_CHANNEL',
    channel: stockConnectChannelSummary(),
    trend: [
      {
        channel: 'SH_NORTHBOUND',
        tradeDate: '2026-07-29',
        dataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
        stats: stockConnectMarketStats(),
        status: stockConnectChannelStatus(),
      },
    ],
    publication: stockConnectPublication(),
  };
}

/** 构造严格的来源活跃证券榜成功响应。 */
export function stockConnectActiveSecurityPage(): Record<string, unknown> {
  return {
    resolvedTradeDate: '2026-07-29',
    dateResolution: 'LATEST_CHANNEL',
    channel: 'SH_NORTHBOUND',
    ranking: 'SOURCE_ACTIVE',
    rankingAvailability: 'REPORTED',
    rankingScope: 'SOURCE_ACTIVE_SECURITIES_ONLY',
    items: [
      {
        rankingRank: 1,
        sourceRank: 1,
        identity: {
          identityAvailability: 'RESOLVED',
          instrumentEntityRef: 'equity:SSE:600000',
          sourceSecurityCode: '600000',
          displayName: '测试证券',
          listingVenue: 'SSE',
        },
        buyAmount: unavailableMoney(),
        sellAmount: unavailableMoney(),
        turnoverAmount: reportedMoney(),
        netBuyAmount: unavailableMoney(),
      },
    ],
    nextCursor: null,
    publication: stockConnectPublication(),
  };
}

/** 构造严格的证券互联互通历史上下文成功响应。 */
export function stockConnectSecurityContextResponse(): Record<string, unknown> {
  return {
    resolvedTradeDate: '2026-07-29',
    identity: {
      identityAvailability: 'RESOLVED',
      instrumentEntityRef: 'equity:SSE:600000',
      sourceSecurityCode: '600000',
      displayName: '测试证券',
      listingVenue: 'SSE',
    },
    activities: [
      {
        channel: 'SH_NORTHBOUND',
        tradeDate: '2026-07-29',
        dataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
        sourceRank: 1,
        turnoverAmount: reportedMoney(),
        netBuyAmount: unavailableMoney(),
      },
    ],
    publication: stockConnectPublication(),
  };
}

/** 按 readiness 合同规则递归排序对象键并保留数组顺序与 Unicode。 */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new Error('测试 readiness 含非 JSON 值。');
    return encoded;
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
    .join(',')}}`;
}
import { createHash } from 'node:crypto';
