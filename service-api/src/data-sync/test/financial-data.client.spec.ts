import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { FinancialDataClient } from '../clients/financial-data.client.js';

/** 提供内部财务调用所需的最小配置。 */
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

const dataVersion = '00000000-0000-4000-8000-000000000001';

/** 覆盖内部 0013 请求、严格合同、公开裁剪、重试与错误映射。 */
describe('FinancialDataClient', () => {
  /** 验证报表头内部血缘被裁剪，并保留查询、缓存和关联字段。 */
  it('reads and projects a financial report page', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(internalReportPage()));
    const client = new FinancialDataClient(config, fetcher);

    const result = await client.listReports({
      exchange: 'SSE',
      symbol: '600519',
      statementTypes: ['INCOME_STATEMENT'],
      periodBases: ['YEAR_TO_DATE'],
      methodologyCode: 'eastmoney.statement',
      methodologyVersion: 1,
      reportPeriodFrom: '2025-01-01',
      reportPeriodTo: '2025-12-31',
      cursor: 'next-page',
      limit: 20,
      ifNoneMatch: '"old"',
      requestId: 'req-report-page',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body.items[0]).not.toHaveProperty('instrumentId');
      expect(result.body.items[0]).not.toHaveProperty('sourceCode');
      expect(result.body.items[0]?.availableFrom).toBe('2026-03-28');
    }
    const [target, init] = fetcher.mock.calls[0] ?? [];
    const url = requestedUrl(target);
    expect(url.pathname).toBe('/internal/v1/equities/SSE/600519/financial-reports');
    expect(url.searchParams.getAll('statementType')).toEqual(['INCOME_STATEMENT']);
    expect(url.searchParams.get('cursor')).toBe('next-page');
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
      'If-None-Match': '"old"',
      'X-Request-Id': 'req-report-page',
    });
  });

  /** 验证财务指标与估值页使用各自受控参数和公开投影。 */
  it('reads platform metrics and valuation observations', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(internalMetricPage()))
      .mockResolvedValueOnce(jsonResponse(internalValuationPage()));
    const client = new FinancialDataClient(config, fetcher);

    const metrics = await client.listMetrics({
      exchange: 'SSE',
      symbol: '600519',
      origin: 'PLATFORM_DERIVED',
      methodologyCode: 'platform.financial-derivation',
      methodologyVersion: 1,
      metrics: ['platform.net_profit_parent.ttm'],
      periodBases: ['TTM'],
      limit: 200,
      requestId: 'req-metrics',
    });
    const valuations = await client.listValuations({
      exchange: 'SSE',
      symbol: '600519',
      methodologyCode: 'eastmoney.valuation',
      methodologyVersion: 1,
      metrics: ['pe_ttm'],
      start: '2025-01-01',
      end: '2025-12-31',
      limit: 500,
      requestId: 'req-valuations',
    });

    expect(metrics.status === 200 && metrics.body.items[0]?.formulaVersion).toBe(1);
    expect(valuations.status === 200 && valuations.body.items[0]?.finality).toBe(
      'PROVIDER_OBSERVATION',
    );
    expect(requestedUrl(fetcher.mock.calls[1]?.[0]).searchParams.get('start')).toBe('2025-01-01');
  });

  /** 验证 304 必须保留 ETag 和 dataVersion，供公开 POST 安全映射为 204。 */
  it('preserves conditional response metadata', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, {
        status: 304,
        headers: { ETag: '"current"', 'X-Data-Version': dataVersion },
      }),
    );
    const client = new FinancialDataClient(config, fetcher);

    const result = await client.getReport({
      exchange: 'SSE',
      symbol: '600519',
      reportRef: '00000000-0000-4000-8000-000000000002',
      limit: 100,
      ifNoneMatch: '"current"',
      requestId: 'req-detail-cache',
    });

    expect(result).toEqual({
      status: 304,
      etag: '"current"',
      dataVersion,
    });
  });

  /** 验证幂等 GET 在一次 503 后重试，合同漂移仍安全映射 503。 */
  it('retries once and rejects invalid internal contracts', async () => {
    const retryFetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(jsonResponse(internalReportPage()));
    const retryClient = new FinancialDataClient(config, retryFetcher);
    await retryClient.listReports({
      exchange: 'SSE',
      symbol: '600519',
      methodologyCode: 'eastmoney.statement',
      methodologyVersion: 1,
      limit: 20,
      requestId: 'req-retry',
    });
    expect(retryFetcher).toHaveBeenCalledTimes(2);

    const invalidClient = new FinancialDataClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse({ ...internalReportPage(), sourceBatchId: 'secret' })),
    );
    await expect(
      invalidClient.listReports({
        exchange: 'SSE',
        symbol: '600519',
        methodologyCode: 'eastmoney.statement',
        methodologyVersion: 1,
        limit: 20,
        requestId: 'req-invalid',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 验证公开冲突映射不泄漏下游 Problem Details。 */
  it('maps cursor conflict safely', async () => {
    const conflictClient = new FinancialDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'internal-code', detail: 'secret' }), {
          status: 409,
          headers: { 'Content-Type': 'application/problem+json' },
        }),
      ),
    );
    await expect(
      conflictClient.listReports({
        exchange: 'SSE',
        symbol: '600519',
        methodologyCode: 'eastmoney.statement',
        methodologyVersion: 1,
        limit: 20,
        requestId: 'req-conflict',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'cursor-mismatch' },
    });
  });

  /** 验证依赖失败只传播有界重试秒数，且弱 ETag 被视为合同漂移。 */
  it('preserves bounded retry advice and rejects weak etags', async () => {
    const unavailableClient = new FinancialDataClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(new Response(null, { status: 503 }))
        .mockResolvedValueOnce(
          new Response(null, { status: 503, headers: { 'Retry-After': '12' } }),
        ),
    );

    await expect(
      unavailableClient.listReports({
        exchange: 'SSE',
        symbol: '600519',
        methodologyCode: 'eastmoney.statement',
        methodologyVersion: 1,
        limit: 20,
        requestId: 'req-unavailable',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable', retryAfter: 12 },
    });

    const weakEtagClient = new FinancialDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(internalReportPage()), {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
            ETag: 'W/"financial-v1"',
            'X-Data-Version': dataVersion,
          },
        }),
      ),
    );
    await expect(
      weakEtagClient.listReports({
        exchange: 'SSE',
        symbol: '600519',
        methodologyCode: 'eastmoney.statement',
        methodologyVersion: 1,
        limit: 20,
        requestId: 'req-weak-etag',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });
});

/** 构造严格内部报表页。 */
function internalReportPage(): Record<string, unknown> {
  return {
    instrumentId: '00000000-0000-4000-8000-000000000010',
    exchange: 'SSE',
    symbol: '600519',
    methodologyCode: 'eastmoney.statement',
    methodologyVersion: 1,
    items: [internalReportHeader()],
    nextCursor: null,
    dataVersion,
    publishedAt: '2026-07-28T00:00:00Z',
    effectiveAsOf: '2026-07-28',
    knowledgeCutoff: '2026-07-28T00:00:00Z',
  };
}

/** 构造严格内部报表头。 */
function internalReportHeader(): Record<string, unknown> {
  return {
    instrumentId: '00000000-0000-4000-8000-000000000010',
    reportRef: '00000000-0000-4000-8000-000000000002',
    exchange: 'SSE',
    symbol: '600519',
    statementType: 'INCOME_STATEMENT',
    reportPeriod: '2025-12-31',
    periodBasis: 'YEAR_TO_DATE',
    statementScope: 'CONSOLIDATED',
    currency: 'CNY',
    currencyNullReason: null,
    reportType: '年度报告',
    auditStatus: 'AUDITED',
    announcementDate: '2026-03-28',
    providerUpdateDate: null,
    effectiveFrom: '2026-03-28',
    effectiveTo: null,
    knownFrom: '2026-03-28T14:10:00Z',
    knownTo: null,
    knowledgeBasis: 'OFFICIAL_ANNOUNCEMENT',
    knowledgeConfidence: 'HIGH',
    observedAt: '2026-03-28T14:10:00Z',
    revision: 1,
    methodologyCode: 'eastmoney.statement',
    methodologyVersion: 1,
    sourceCode: 'eastmoney.financial',
    qualityStatus: 'PASSED',
  };
}

/** 构造严格内部平台派生指标页。 */
function internalMetricPage(): Record<string, unknown> {
  return {
    instrumentId: '00000000-0000-4000-8000-000000000010',
    exchange: 'SSE',
    symbol: '600519',
    origin: 'PLATFORM_DERIVED',
    methodologyCode: 'platform.financial-derivation',
    methodologyVersion: 1,
    items: [
      {
        metricCode: 'platform.net_profit_parent.ttm',
        label: '归母净利润 TTM',
        origin: 'PLATFORM_DERIVED',
        reportPeriod: '2025-12-31',
        periodBasis: 'TTM',
        statementScope: 'CONSOLIDATED',
        value: '100.00',
        unit: 'CNY',
        currency: 'CNY',
        currencyNullReason: null,
        methodologyCode: 'platform.financial-derivation',
        methodologyVersion: 1,
        formulaVersion: 1,
        effectiveFrom: '2026-03-28',
        knownFrom: '2026-03-28T14:10:00Z',
        knowledgeBasis: 'OBSERVED_AT',
        knowledgeConfidence: 'CONSERVATIVE',
        observedAt: '2026-03-28T14:10:00Z',
        revision: 1,
      },
    ],
    nextCursor: null,
    dataVersion,
    publishedAt: '2026-07-28T00:00:00Z',
    effectiveAsOf: '2026-07-28',
    knowledgeCutoff: '2026-07-28T00:00:00Z',
  };
}

/** 构造严格内部估值页。 */
function internalValuationPage(): Record<string, unknown> {
  return {
    instrumentId: '00000000-0000-4000-8000-000000000010',
    exchange: 'SSE',
    symbol: '600519',
    methodologyCode: 'eastmoney.valuation',
    methodologyVersion: 1,
    items: [
      {
        observationDate: '2025-12-31',
        metricCode: 'pe_ttm',
        value: '20.1',
        unit: 'ratio',
        currency: null,
        currencyNullReason: 'NOT_APPLICABLE',
        methodologyCode: 'eastmoney.valuation',
        methodologyVersion: 1,
        finality: 'PROVIDER_OBSERVATION',
        effectiveFrom: '2026-01-01',
        knownFrom: '2026-01-01T08:00:00Z',
        knowledgeBasis: 'OBSERVED_AT',
        knowledgeConfidence: 'CONSERVATIVE',
        observedAt: '2026-01-01T08:00:00Z',
        revision: 1,
      },
    ],
    nextCursor: null,
    dataVersion,
    publishedAt: '2026-07-28T00:00:00Z',
    effectiveAsOf: '2026-07-28',
    knowledgeCutoff: '2026-07-28T00:00:00Z',
  };
}

/** 构造带强 ETag 与 dataVersion 的内部 JSON 成功响应。 */
function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      ETag: '"financial-v1"',
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
