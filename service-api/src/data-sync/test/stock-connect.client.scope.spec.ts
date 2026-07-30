import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { StockConnectClient } from '../clients/stock-connect.client.js';
import {
  STOCK_CONNECT_TEST_DATA_VERSION,
  derivedMoney,
  reportedMoney,
  stockConnectActiveSecurityPage,
  stockConnectChannelResponse,
  stockConnectChannelStatus,
  stockConnectChannelSummary,
  stockConnectMarketStats,
  stockConnectOverviewResponse,
  stockConnectPublication,
  stockConnectSecurityContextResponse,
  unavailableMoney,
} from './stock-connect.test-data.js';

/** 提供请求关联校验所需的最小同步服务配置。 */
const config = {
  dataSyncStockConnectBaseUrl: 'http://data-sync-api:8000',
  dataSyncStockConnectBearerToken: 'test-only-stock-connect-read-token-000000000000000000',
  dataSyncStockConnectTimeoutMs: 3_000,
  dataSyncStockConnectCircuitFailures: 5,
  dataSyncStockConnectCircuitWindowMs: 30_000,
  dataSyncStockConnectCircuitOpenMs: 30_000,
} as AppConfigService;

/** 覆盖成功响应与请求通道、日期、身份、排行、币种和净额符号的强关联。 */
describe('StockConnectClient response scope', () => {
  /** 验证总览逐日趋势必须无重复地覆盖请求中的每一条通道。 */
  it('requires an exact channel set and complete daily overview matrix', async () => {
    const complete = stockConnectOverviewResponse();
    complete.channels = [stockConnectChannelSummary(), northShenzhenSummary()];
    complete.trend = [trendPoint('SH_NORTHBOUND', 'CNY'), trendPoint('SZ_NORTHBOUND', 'CNY')];
    const acceptedClient = clientReturning(complete, 'req-overview-complete');

    await expect(
      acceptedClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND', 'SZ_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-overview-complete',
      ),
    ).resolves.toMatchObject({ dataVersion: STOCK_CONNECT_TEST_DATA_VERSION });

    const missingSummary = stockConnectOverviewResponse();
    const missingSummaryClient = clientReturning(missingSummary, 'req-overview-missing-summary');
    await expect(
      missingSummaryClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND', 'SZ_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-overview-missing-summary',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const incomplete = stockConnectOverviewResponse();
    incomplete.channels = [stockConnectChannelSummary(), northShenzhenSummary()];
    incomplete.trend = [trendPoint('SH_NORTHBOUND', 'CNY')];
    const incompleteClient = clientReturning(incomplete, 'req-overview-incomplete');
    await expect(
      incompleteClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND', 'SZ_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-overview-incomplete',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const duplicate = stockConnectOverviewResponse();
    duplicate.channels = [stockConnectChannelSummary(), stockConnectChannelSummary()];
    duplicate.trend = [trendPoint('SH_NORTHBOUND', 'CNY'), trendPoint('SH_NORTHBOUND', 'CNY')];
    const duplicateClient = clientReturning(duplicate, 'req-overview-duplicate');
    await expect(
      duplicateClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-overview-duplicate',
      ),
    ).rejects.toMatchObject(dependencyFailure());
  });

  /** 验证总览趋势必须覆盖解析日、不得越界，并受请求交易日窗口约束。 */
  it('binds overview trend dates, limits and current publication version', async () => {
    const missingCurrent = stockConnectOverviewResponse();
    missingCurrent.trend = [trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-28')];
    const missingCurrentClient = clientReturning(missingCurrent, 'req-overview-missing-current');
    await expect(
      missingCurrentClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-overview-missing-current',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const future = stockConnectOverviewResponse();
    future.trend = [trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-30')];
    const futureClient = clientReturning(future, 'req-overview-future');
    await expect(
      futureClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-overview-future',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const overLimit = stockConnectOverviewResponse();
    overLimit.trend = [
      trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-28'),
      trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-29'),
    ];
    const overLimitClient = clientReturning(overLimit, 'req-overview-over-limit');
    await expect(
      overLimitClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 1,
        },
        'req-overview-over-limit',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongVersion = stockConnectOverviewResponse();
    wrongVersion.trend = [
      trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-29', 'another-current-version'),
    ];
    const wrongVersionClient = clientReturning(wrongVersion, 'req-overview-current-version');
    await expect(
      wrongVersionClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-overview-current-version',
      ),
    ).rejects.toMatchObject(dependencyFailure());
  });

  /** 验证摘要日期和通道固定方向、交易所路由不能被形状正确的错误数据替换。 */
  it('rejects overview summaries with a wrong date or channel semantics', async () => {
    const wrongDate = stockConnectOverviewResponse();
    wrongDate.channels = [{ ...stockConnectChannelSummary(), tradeDate: '2026-07-28' }];
    const wrongDateClient = clientReturning(wrongDate, 'req-summary-date');
    await expect(
      wrongDateClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-summary-date',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongDirection = stockConnectOverviewResponse();
    wrongDirection.channels = [
      { ...stockConnectChannelSummary(), direction: 'SOUTHBOUND', route: 'SHENZHEN' },
    ];
    const wrongDirectionClient = clientReturning(wrongDirection, 'req-summary-semantics');
    await expect(
      wrongDirectionClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-summary-semantics',
      ),
    ).rejects.toMatchObject(dependencyFailure());
  });

  /** 验证单通道详情不能返回其他通道或在北向业务中夹带港币金额。 */
  it('rejects a channel response with another channel or mixed currency', async () => {
    const anotherChannel = stockConnectChannelResponse();
    anotherChannel.channel = northShenzhenSummary();
    anotherChannel.trend = [trendPoint('SZ_NORTHBOUND', 'CNY')];
    const channelClient = clientReturning(anotherChannel, 'req-channel-scope');
    await expect(
      channelClient.channel(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 20,
        },
        'req-channel-scope',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const mixedCurrency = stockConnectChannelResponse();
    const badStats = marketStats('HKD');
    mixedCurrency.channel = { ...stockConnectChannelSummary(), stats: badStats };
    mixedCurrency.trend = [trendPoint('SH_NORTHBOUND', 'HKD')];
    const currencyClient = clientReturning(mixedCurrency, 'req-channel-currency');
    await expect(
      currencyClient.channel(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 20,
        },
        'req-channel-currency',
      ),
    ).rejects.toMatchObject(dependencyFailure());
  });

  /** 验证通道趋势逐日唯一、覆盖解析日，且不得超过日期与 publication 边界。 */
  it('binds channel trend dates, uniqueness, limits and current version', async () => {
    const duplicate = stockConnectChannelResponse();
    duplicate.trend = [trendPoint('SH_NORTHBOUND', 'CNY'), trendPoint('SH_NORTHBOUND', 'CNY')];
    const duplicateClient = clientReturning(duplicate, 'req-channel-duplicate');
    await expect(
      duplicateClient.channel(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 20,
        },
        'req-channel-duplicate',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const missingCurrent = stockConnectChannelResponse();
    missingCurrent.trend = [trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-28')];
    const missingCurrentClient = clientReturning(missingCurrent, 'req-channel-missing-current');
    await expect(
      missingCurrentClient.channel(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 20,
        },
        'req-channel-missing-current',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const future = stockConnectChannelResponse();
    future.trend = [trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-30')];
    const futureClient = clientReturning(future, 'req-channel-future');
    await expect(
      futureClient.channel(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 20,
        },
        'req-channel-future',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const overLimit = stockConnectChannelResponse();
    overLimit.trend = [
      trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-28'),
      trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-29'),
    ];
    const overLimitClient = clientReturning(overLimit, 'req-channel-over-limit');
    await expect(
      overLimitClient.channel(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 1,
        },
        'req-channel-over-limit',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongVersion = stockConnectChannelResponse();
    wrongVersion.trend = [
      trendPoint('SH_NORTHBOUND', 'CNY', '2026-07-29', 'another-current-version'),
    ];
    const wrongVersionClient = clientReturning(wrongVersion, 'req-channel-current-version');
    await expect(
      wrongVersionClient.channel(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 20,
        },
        'req-channel-current-version',
      ),
    ).rejects.toMatchObject(dependencyFailure());
  });

  /** 验证净买净卖符号、父 publication、请求排行和活跃金额原币全部强绑定。 */
  it('rejects active-security scope, sign, parent version and currency mismatches', async () => {
    const wrongRankingClient = clientReturning(
      stockConnectActiveSecurityPage(),
      'req-active-ranking',
    );
    await expect(
      wrongRankingClient.activeSecurities(activeQuery('NET_BUY'), 'req-active-ranking'),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongParentClient = clientReturning(
      stockConnectActiveSecurityPage(),
      'req-active-parent',
    );
    await expect(
      wrongParentClient.activeSecurities(
        {
          ...activeQuery('SOURCE_ACTIVE'),
          parentPublicationDataVersion: 'another-parent-publication',
        },
        'req-active-parent',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongBuySign = netRankingPage('NET_BUY', '-1.00', 'CNY');
    const wrongBuyClient = clientReturning(wrongBuySign, 'req-active-buy-sign');
    await expect(
      wrongBuyClient.activeSecurities(activeQuery('NET_BUY'), 'req-active-buy-sign'),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongSellSign = netRankingPage('NET_SELL', '1.00', 'CNY');
    const wrongSellClient = clientReturning(wrongSellSign, 'req-active-sell-sign');
    await expect(
      wrongSellClient.activeSecurities(activeQuery('NET_SELL'), 'req-active-sell-sign'),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongCurrency = netRankingPage('NET_BUY', '1.00', 'HKD');
    const wrongCurrencyClient = clientReturning(wrongCurrency, 'req-active-currency');
    await expect(
      wrongCurrencyClient.activeSecurities(activeQuery('NET_BUY'), 'req-active-currency'),
    ).rejects.toMatchObject(dependencyFailure());
  });

  /** 验证活跃榜 latest 可分别继承总览共同日与单通道父 publication。 */
  it('accepts active rankings for overview and channel parent publications', async () => {
    const overviewParentPage = stockConnectActiveSecurityPage();
    overviewParentPage.dateResolution = 'LATEST_COMMON';
    const overviewParentClient = clientReturning(overviewParentPage, 'req-active-overview-parent');
    await expect(
      overviewParentClient.activeSecurities(
        activeQuery('SOURCE_ACTIVE'),
        'req-active-overview-parent',
      ),
    ).resolves.toMatchObject({ dataVersion: STOCK_CONNECT_TEST_DATA_VERSION });

    const channelParentClient = clientReturning(
      stockConnectActiveSecurityPage(),
      'req-active-channel-parent',
    );
    await expect(
      channelParentClient.activeSecurities(
        activeQuery('SOURCE_ACTIVE'),
        'req-active-channel-parent',
      ),
    ).resolves.toMatchObject({ dataVersion: STOCK_CONNECT_TEST_DATA_VERSION });
  });

  /** 验证券上下文拒绝未解析身份、错误实体、错误筛选通道和混合业务币种。 */
  it('requires a resolved matching security identity and scoped activities', async () => {
    const unresolved = stockConnectSecurityContextResponse();
    unresolved.identity = {
      identityAvailability: 'SOURCE_UNRESOLVED',
      instrumentEntityRef: null,
      sourceSecurityCode: '600000',
      displayName: null,
      listingVenue: 'SSE',
    };
    const unresolvedClient = clientReturning(unresolved, 'req-security-unresolved');
    await expect(
      unresolvedClient.securityContext(securityQuery('SH_NORTHBOUND'), 'req-security-unresolved'),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongEntity = stockConnectSecurityContextResponse();
    wrongEntity.identity = {
      identityAvailability: 'RESOLVED',
      instrumentEntityRef: 'equity:SSE:600001',
      sourceSecurityCode: '600001',
      displayName: null,
      listingVenue: 'SSE',
    };
    const entityClient = clientReturning(wrongEntity, 'req-security-entity');
    await expect(
      entityClient.securityContext(securityQuery('SH_NORTHBOUND'), 'req-security-entity'),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongChannel = stockConnectSecurityContextResponse();
    wrongChannel.activities = [securityActivity('SZ_NORTHBOUND', 'CNY')];
    const channelClient = clientReturning(wrongChannel, 'req-security-channel');
    await expect(
      channelClient.securityContext(securityQuery('SH_NORTHBOUND'), 'req-security-channel'),
    ).rejects.toMatchObject(dependencyFailure());

    const wrongCurrency = stockConnectSecurityContextResponse();
    wrongCurrency.activities = [securityActivity('SH_NORTHBOUND', 'HKD')];
    const currencyClient = clientReturning(wrongCurrency, 'req-security-currency');
    await expect(
      currencyClient.securityContext(securityQuery('SH_NORTHBOUND'), 'req-security-currency'),
    ).rejects.toMatchObject(dependencyFailure());
  });

  /** 验证券历史逐通道日唯一、受日期窗口约束，并共享同日共同 publication。 */
  it('binds security history dates, uniqueness, limits and same-day versions', async () => {
    const duplicate = stockConnectSecurityContextResponse();
    duplicate.activities = [
      securityActivity('SH_NORTHBOUND', 'CNY'),
      securityActivity('SH_NORTHBOUND', 'CNY'),
    ];
    const duplicateClient = clientReturning(duplicate, 'req-security-duplicate');
    await expect(
      duplicateClient.securityContext(securityQuery('SH_NORTHBOUND'), 'req-security-duplicate'),
    ).rejects.toMatchObject(dependencyFailure());

    const overLimit = stockConnectSecurityContextResponse();
    overLimit.activities = [
      securityActivity('SH_NORTHBOUND', 'CNY', '2026-07-28'),
      securityActivity('SH_NORTHBOUND', 'CNY', '2026-07-29'),
    ];
    const overLimitClient = clientReturning(overLimit, 'req-security-over-limit');
    await expect(
      overLimitClient.securityContext(
        {
          ...securityQuery('SH_NORTHBOUND'),
          historyTradingDays: 1,
        },
        'req-security-over-limit',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const future = stockConnectSecurityContextResponse();
    future.activities = [securityActivity('SH_NORTHBOUND', 'CNY', '2026-07-30')];
    const futureClient = clientReturning(future, 'req-security-future');
    await expect(
      futureClient.securityContext(securityQuery('SH_NORTHBOUND'), 'req-security-future'),
    ).rejects.toMatchObject(dependencyFailure());

    const mixedVersions = stockConnectSecurityContextResponse();
    mixedVersions.activities = [
      securityActivity('SH_NORTHBOUND', 'CNY'),
      securityActivity('SZ_NORTHBOUND', 'CNY', '2026-07-29', 'another-day-version'),
    ];
    const mixedVersionsClient = clientReturning(mixedVersions, 'req-security-mixed-version');
    await expect(
      mixedVersionsClient.securityContext(securityQuery(null), 'req-security-mixed-version'),
    ).rejects.toMatchObject(dependencyFailure());

    const commonVersion = stockConnectSecurityContextResponse();
    commonVersion.activities = [
      securityActivity('SH_NORTHBOUND', 'CNY'),
      securityActivity('SZ_NORTHBOUND', 'CNY'),
    ];
    const commonVersionClient = clientReturning(commonVersion, 'req-security-common-version');
    await expect(
      commonVersionClient.securityContext(securityQuery(null), 'req-security-common-version'),
    ).resolves.toMatchObject({ dataVersion: STOCK_CONNECT_TEST_DATA_VERSION });
  });

  /** 验证四类 EXACT 查询均拒绝同步服务返回其他解析交易日。 */
  it('requires every exact response to resolve to the requested trade date', async () => {
    const overviewClient = clientReturning(stockConnectOverviewResponse(), 'req-exact-overview');
    await expect(
      overviewClient.overview(
        {
          date: { mode: 'EXACT', exactDate: '2026-07-28' },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-exact-overview',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const channelClient = clientReturning(stockConnectChannelResponse(), 'req-exact-channel');
    await expect(
      channelClient.channel(
        {
          date: { mode: 'EXACT', exactDate: '2026-07-28' },
          channel: 'SH_NORTHBOUND',
          trendTradingDays: 20,
        },
        'req-exact-channel',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const activeClient = clientReturning(stockConnectActiveSecurityPage(), 'req-exact-active');
    await expect(
      activeClient.activeSecurities(
        {
          ...activeQuery('SOURCE_ACTIVE'),
          date: { mode: 'EXACT', exactDate: '2026-07-28' },
        },
        'req-exact-active',
      ),
    ).rejects.toMatchObject(dependencyFailure());

    const securityClient = clientReturning(
      stockConnectSecurityContextResponse(),
      'req-exact-security',
    );
    await expect(
      securityClient.securityContext(
        {
          ...securityQuery('SH_NORTHBOUND'),
          date: { mode: 'EXACT', exactDate: '2026-07-28' },
        },
        'req-exact-security',
      ),
    ).rejects.toMatchObject(dependencyFailure());
  });
});

/** 构造指向一份固定成功正文的严格内部客户端。 */
function clientReturning(body: Record<string, unknown>, requestId: string): StockConnectClient {
  return new StockConnectClient(
    config,
    vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body, requestId)),
  );
}

/** 构造带关联请求标识和不可变 publication 版本的成功响应。 */
function jsonResponse(body: Record<string, unknown>, requestId: string): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'X-Request-Id': requestId,
      'X-Data-Version': STOCK_CONNECT_TEST_DATA_VERSION,
    },
  });
}

/** 构造公开依赖失败断言，避免测试依赖内部错误详情。 */
function dependencyFailure(): {
  status: HttpStatus;
  response: { code: string };
} {
  return {
    status: HttpStatus.SERVICE_UNAVAILABLE,
    response: { code: 'UPSTREAM_UNAVAILABLE' },
  };
}

/** 构造沪股通以外的另一条北向通道摘要。 */
function northShenzhenSummary(): Record<string, unknown> {
  return {
    ...stockConnectChannelSummary(),
    channel: 'SZ_NORTHBOUND',
    direction: 'NORTHBOUND',
    route: 'SHENZHEN',
  };
}

/** 构造指定通道和业务币种的趋势点。 */
function trendPoint(
  channel: 'SH_NORTHBOUND' | 'SZ_NORTHBOUND',
  currency: 'CNY' | 'HKD',
  tradeDate = '2026-07-29',
  dataVersion = STOCK_CONNECT_TEST_DATA_VERSION,
): Record<string, unknown> {
  return {
    channel,
    tradeDate,
    dataVersion,
    stats: marketStats(currency),
    status: stockConnectChannelStatus(),
  };
}

/** 构造金额原币可控的完整市场统计。 */
function marketStats(currency: 'CNY' | 'HKD'): Record<string, unknown> {
  return {
    ...stockConnectMarketStats(),
    turnoverAmount: reportedMoney('100.00', currency),
    etfTurnoverAmount: reportedMoney('20.00', currency),
  };
}

/** 构造净额排行成功字段形状，供符号和币种关联测试使用。 */
function netRankingPage(
  ranking: 'NET_BUY' | 'NET_SELL',
  netAmount: string,
  currency: 'CNY' | 'HKD',
): Record<string, unknown> {
  return {
    resolvedTradeDate: '2026-07-29',
    dateResolution: 'LATEST_CHANNEL',
    channel: 'SH_NORTHBOUND',
    ranking,
    rankingAvailability: 'DERIVED',
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
        turnoverAmount: reportedMoney('100.00', currency),
        netBuyAmount: derivedMoney(netAmount, currency),
      },
    ],
    nextCursor: null,
    publication: stockConnectPublication(),
  };
}

/** 构造一个榜单请求，并固定其父 publication 版本。 */
function activeQuery(ranking: 'SOURCE_ACTIVE' | 'NET_BUY' | 'NET_SELL'): Record<string, unknown> {
  return {
    date: { mode: 'LATEST', exactDate: null },
    channel: 'SH_NORTHBOUND',
    ranking,
    parentPublicationDataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
    cursor: null,
    limit: 20,
  };
}

/** 构造证券上下文请求，显式保留可空通道筛选。 */
function securityQuery(channel: 'SH_NORTHBOUND' | 'SZ_NORTHBOUND' | null): Record<string, unknown> {
  return {
    instrumentEntityRef: 'equity:SSE:600000',
    date: { mode: 'LATEST', exactDate: null },
    channel,
    historyTradingDays: 20,
  };
}

/** 构造币种可控的证券通道活动。 */
function securityActivity(
  channel: 'SH_NORTHBOUND' | 'SZ_NORTHBOUND',
  currency: 'CNY' | 'HKD',
  tradeDate = '2026-07-29',
  dataVersion = STOCK_CONNECT_TEST_DATA_VERSION,
): Record<string, unknown> {
  return {
    channel,
    tradeDate,
    dataVersion,
    sourceRank: 1,
    turnoverAmount: reportedMoney('100.00', currency),
    netBuyAmount: unavailableMoney(),
  };
}
