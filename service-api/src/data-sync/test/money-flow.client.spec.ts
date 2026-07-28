import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { MoneyFlowClient } from '../clients/money-flow.client.js';

const dataVersion = '00000000-0000-4000-8000-000000000017';

/** 提供内部资金流调用所需的最小配置。 */
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

/** 覆盖内部 0015 请求、严格契约、公开裁剪、重试与条件响应。 */
describe('MoneyFlowClient', () => {
  /** 验证目录只公开生产可用方法学，并移除 adapter 与内部 UUID。 */
  it('filters and projects production-enabled methodologies', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(methodologyPage()));
    const client = new MoneyFlowClient(config, fetcher);

    const result = await client.listMethodologies({
      semanticFamily: 'trade_direction_flow',
      scopeType: 'market',
      limit: 20,
      ifNoneMatch: '"old"',
      requestId: 'req-methodologies',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body.items).toHaveLength(1);
      expect(result.body.items[0]).not.toHaveProperty('methodologyUuid');
      expect(result.body.items[0]).not.toHaveProperty('adapterProvider');
      expect(result.body.items[0]?.buckets[0]?.definitionStatus).toBe('unknown');
    }
    const [target, init] = fetcher.mock.calls[0] ?? [];
    const url = requestedUrl(target);
    expect(url.pathname).toBe('/internal/v1/money-flow/methodologies');
    expect(url.searchParams.get('methodologyStatus')).toBe('validated');
    expect(url.searchParams.get('semanticFamily')).toBe('trade_direction_flow');
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
      'If-None-Match': '"old"',
      'X-Request-Id': 'req-methodologies',
    });
  });

  /** 验证证券日序列移除内部强身份键，并保留双时间观察字段。 */
  it('projects an equity daily series without internal identifiers', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(dailyPage()));
    const client = new MoneyFlowClient(config, fetcher);

    const result = await client.listDaily({
      methodologyId: 'eastmoney.trade-direction',
      methodologyVersion: '1',
      scopePath: 'equities/SSE/600519',
      bucket: 'main',
      start: '2026-07-01',
      end: '2026-07-28',
      knownAt: '2026-07-28T08:00:00Z',
      limit: 200,
      requestId: 'req-daily',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body).not.toHaveProperty('seriesId');
      expect(result.body.scope).not.toHaveProperty('securityId');
      expect(result.body.scope).not.toHaveProperty('instrumentId');
      expect(result.body.items[0]?.knownFrom).toBe('2026-07-28T08:00:00Z');
    }
    const url = requestedUrl(fetcher.mock.calls[0]?.[0]);
    expect(url.pathname).toBe(
      '/internal/v1/money-flow/methodologies/eastmoney.trade-direction/daily-series/equities/SSE/600519',
    );
    expect(url.searchParams.get('knownAt')).toBe('2026-07-28T08:00:00Z');
  });

  /** 验证内部 304 保留版本头，且一次 503 后仅重试一次。 */
  it('preserves conditional metadata and retries one transient failure', async () => {
    const conditionalClient = new MoneyFlowClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(null, {
          status: 304,
          headers: { ETag: '"money-flow-v1"', 'X-Data-Version': dataVersion },
        }),
      ),
    );
    await expect(
      conditionalClient.listRanking({
        methodologyId: 'eastmoney.order-size',
        methodologyVersion: '1',
        scopeType: 'equity',
        universe: 'cn-a',
        windowType: 'supplier_day',
        windowSize: 1,
        bucket: 'main',
        limit: 100,
        requestId: 'req-cache',
      }),
    ).resolves.toEqual({
      status: 304,
      etag: '"money-flow-v1"',
      dataVersion,
    });

    const retryFetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(jsonResponse(methodologyPage()));
    const retryClient = new MoneyFlowClient(config, retryFetcher);
    await retryClient.listMethodologies({ limit: 50, requestId: 'req-retry' });
    expect(retryFetcher).toHaveBeenCalledTimes(2);
  });

  /** 验证契约漂移与身份边界分别安全映射为 503 和 409。 */
  it('fails closed on contract drift and maps identity conflicts', async () => {
    const invalidClient = new MoneyFlowClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse({ ...dailyPage(), providerSecret: 'hidden' })),
    );
    await expect(
      invalidClient.listDaily({
        methodologyId: 'eastmoney.trade-direction',
        methodologyVersion: '1',
        scopePath: 'markets/cn-a',
        bucket: 'main',
        start: '2026-07-01',
        end: '2026-07-28',
        limit: 200,
        requestId: 'req-invalid',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });

    const conflictClient = new MoneyFlowClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 409 })),
    );
    await expect(
      conflictClient.listRanking({
        methodologyId: 'eastmoney.order-size',
        methodologyVersion: '1',
        scopeType: 'equity',
        universe: 'cn-a',
        windowType: 'supplier_day',
        windowSize: 1,
        bucket: 'main',
        limit: 100,
        requestId: 'req-conflict',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'query-conflict' },
    });
  });
});

/** 构造包含一个生产方法和一个研究方法的严格内部目录。 */
function methodologyPage(): Record<string, unknown> {
  return {
    dataVersion,
    publishedAt: '2026-07-28T08:00:00Z',
    items: [
      methodology('eastmoney.trade-direction', 'validated', true),
      methodology('eastmoney.research-only', 'research', false),
    ],
    nextCursor: null,
  };
}

/** 构造一条严格内部方法学定义。 */
function methodology(
  methodologyId: string,
  methodologyStatus: string,
  productionEnabled: boolean,
): Record<string, unknown> {
  return {
    methodologyUuid: '00000000-0000-4000-8000-000000000018',
    methodologyId,
    methodologyVersion: '1',
    methodologyStatus,
    productionEnabled,
    adapterProvider: 'akshare',
    upstreamSource: 'eastmoney',
    sourceDataset: 'stock_market_fund_flow',
    semanticFamily: 'trade_direction_flow',
    scopeTypes: ['market'],
    universeIds: ['cn-a'],
    supportedWindows: [{ windowType: 'daily_source', windowSize: 1, label: '来源日值' }],
    buckets: [
      {
        bucket: 'main',
        label: '主力',
        definitionStatus: 'inferred_unapproved',
        thresholdMin: null,
        thresholdMax: null,
        thresholdUnit: null,
      },
    ],
    supportedMeasures: ['net_amount', 'net_ratio'],
    ratioDenominator: '来源成交额',
    directionDefinition: '来源定义的主动方向净流入',
    finality: 'post_close_observation',
    currency: 'CNY',
    rawAmountUnit: 'CNY',
    standardAmountUnit: 'CNY',
    conversionVersion: 'identity-v1',
    effectiveFrom: '2026-07-28T08:00:00Z',
    retiredAt: null,
  };
}

/** 构造包含内部证券身份的严格日序列页。 */
function dailyPage(): Record<string, unknown> {
  return {
    seriesId: '00000000-0000-4000-8000-000000000019',
    methodologyId: 'eastmoney.trade-direction',
    methodologyVersion: '1',
    upstreamSource: 'eastmoney',
    sourceDataset: 'stock_individual_fund_flow',
    semanticFamily: 'trade_direction_flow',
    scope: {
      scopeType: 'equity',
      securityId: 1,
      instrumentId: '00000000-0000-4000-8000-000000000020',
      exchange: 'SSE',
      symbol: '600519',
      name: '贵州茅台',
    },
    universe: 'cn-a',
    bucket: 'main',
    supportedMeasures: ['net_amount', 'net_ratio'],
    ratioDenominator: '来源成交额',
    directionDefinition: '来源定义的主动方向净流入',
    windowType: 'daily_source',
    windowSize: 1,
    currency: 'CNY',
    amountUnit: 'CNY',
    knownAtApplied: '2026-07-28T08:00:00Z',
    dataVersion,
    publishedAt: '2026-07-28T08:00:00Z',
    items: [
      {
        tradeDate: '2026-07-28',
        observedAt: '2026-07-28T08:00:00Z',
        knownFrom: '2026-07-28T08:00:00Z',
        finality: 'post_close_observation',
        grossInflow: null,
        grossOutflow: null,
        netAmount: '100.00',
        netRatio: '1.5',
        qualityStatus: 'passed',
      },
    ],
    nextCursor: null,
  };
}

/** 构造带强 ETag 和 dataVersion 的内部成功响应。 */
function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      ETag: '"money-flow-v1"',
      'X-Data-Version': dataVersion,
    },
  });
}

/** 将 fetch 目标统一解析为 URL。 */
function requestedUrl(target: Parameters<typeof fetch>[0] | undefined): URL {
  if (target instanceof URL) return target;
  if (typeof target === 'string') return new URL(target);
  if (target instanceof Request) return new URL(target.url);
  throw new Error('Expected fetch to receive a request target');
}
