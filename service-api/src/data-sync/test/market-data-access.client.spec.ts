import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { MarketDataAccessClient } from '../clients/market-data-access.client.js';

/** 提供通用市场数据内部 POST 调用所需的最小配置。 */
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

const REQUEST_ID = 'market/data:request-101';
const ENTITY_REF = '00000000-0000-4000-8000-000000000102';
const DATA_VERSION = '00000000-0000-4000-8000-000000000103';
const SELECTED_DATA_VERSION = '00000000-0000-4000-8000-000000000104';

/** 表示可定向制造 response-binding 漂移的 ETF AVAILABLE 响应。 */
type MutableEtfAvailableResponse = Record<string, unknown> & {
  meta: Record<string, unknown> & {
    visibility: Record<string, unknown>;
    page: {
      limit: number;
      hasMore: boolean;
      nextCursor: string | null;
    };
    release: Record<string, unknown> & {
      dataVersion: string;
      quality: { status: string; issueCodes: string[] };
      sources: Array<Record<string, unknown>>;
    };
  };
  records: Array<Record<string, unknown>>;
};

/** 覆盖成功空页的内部 POST 方法、服务身份和严格合同投影。 */
describe('MarketDataAccessClient', () => {
  /** 未发布数据集必须被接受为可显示的空 records，而非被错误映射为 503。 */
  it('forwards a query and accepts a successful source-unavailable empty page', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          meta: {
            requestId: REQUEST_ID,
            contractVersion: '1.0.0',
            dataset: { code: 'derivative.bar.1d.reported', schemaVersion: 1 },
            availability: 'SOURCE_UNAVAILABLE',
            release: {
              state: 'SOURCE_UNAVAILABLE',
              observedAt: null,
              reasonCode: 'PUBLICATION_NOT_AVAILABLE',
            },
            visibility: { mode: 'CURRENT' },
            page: { limit: 100, hasMore: false, nextCursor: null },
            coverage: { from: null, to: null, pitCoverage: 'UNKNOWN', gaps: [] },
            warnings: ['publication_unavailable'],
            disclaimers: [],
          },
          records: [],
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Request-Id': REQUEST_ID },
        },
      ),
    );
    const client = new MarketDataAccessClient(config, fetcher);

    const result = await client.query({ request: requestBody(), requestId: REQUEST_ID });

    expect(result.meta.availability).toBe('SOURCE_UNAVAILABLE');
    expect(result.records).toEqual([]);
    const [target, init] = fetcher.mock.calls[0] ?? [];
    if (target === undefined) throw new Error('market-data client must issue one request');
    expect(new URL(target instanceof Request ? target.url : target).pathname).toBe(
      '/internal/v1/market-data/query',
    );
    expect(init).toMatchObject({
      method: 'POST',
      headers: {
        Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
        'Content-Type': 'application/json',
        'X-Request-Id': REQUEST_ID,
      },
    });
    if (typeof init?.body !== 'string')
      throw new Error('market-data request body must be JSON text');
    expect(JSON.parse(init.body)).toMatchObject({
      dataset: { code: 'derivative.bar.1d.reported', schemaVersion: 1 },
    });
  });

  /** ETF v2 请求在发起网络调用前执行字段、筛选和分页白名单校验。 */
  it('fails fast on an invalid ETF v2 query', async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = new MarketDataAccessClient(config, fetcher);
    const invalid = { ...etfBarRequestBody(), page: { limit: 367 } };

    await expect(client.query({ request: invalid, requestId: REQUEST_ID })).rejects.toMatchObject({
      status: HttpStatus.BAD_REQUEST,
      response: { code: 'validation-error' },
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  /** ETF v2 record 扁平化或字段漂移必须收敛为安全 503，不能泄漏到 Web。 */
  it('rejects a malformed ETF v2 downstream record', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(malformedEtfAvailableResponse()), {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
          'X-Data-Version': DATA_VERSION,
          'X-Request-Id': REQUEST_ID,
        },
      }),
    );
    const client = new MarketDataAccessClient(config, fetcher);

    await expect(
      client.query({ request: etfBarRequestBody(), requestId: REQUEST_ID }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** publication 夹带 raw URI 等内部字段时必须整体拒绝，不能从公开边界透传。 */
  it('rejects private fields in an available downstream release', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify(
          etfAvailableResponse({
            rawPayloadUri: 's3://private-market-data/etf/2026-07-30.json',
          }),
        ),
        {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
            'X-Data-Version': DATA_VERSION,
            'X-Request-Id': REQUEST_ID,
          },
        },
      ),
    );
    const client = new MarketDataAccessClient(config, fetcher);

    await expect(
      client.query({ request: etfBarRequestBody(), requestId: REQUEST_ID }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** ETF v2 下游响应必须绑定请求可见性、分页、质量、精确版本和单一冻结来源。 */
  it('maps every ETF v2 response-binding drift to a safe dependency error', async () => {
    const visibilityDrift = etfAvailableResponse();
    visibilityDrift.meta.visibility = { mode: 'PUBLIC_PIT' };
    const pageLimitDrift = etfAvailableResponse();
    pageLimitDrift.meta.page.limit = 365;
    const cardinalityDrift = etfAvailableResponse();
    cardinalityDrift.meta.page.limit = 1;
    const repeated = cardinalityDrift.records[0];
    if (repeated === undefined) throw new Error('ETF response fixture must contain one record');
    cardinalityDrift.records.push({
      ...repeated,
      recordRef: `etf-bar:${ENTITY_REF}:2026-07-29:1`,
    });
    const cursorDrift = etfAvailableResponse();
    cursorDrift.meta.page.hasMore = true;
    const qualityDrift = etfAvailableResponse();
    qualityDrift.meta.release.quality.status = 'WARNED';
    const sourceDrift = etfAvailableResponse();
    const primarySource = sourceDrift.meta.release.sources[0];
    if (primarySource === undefined)
      throw new Error('ETF response fixture must contain one source');
    sourceDrift.meta.release.sources.push({
      ...primarySource,
      sourceRef: 'src_etf_shadow',
    });
    const cases = [
      {
        label: 'visibility',
        request: etfBarRequestBody(),
        response: visibilityDrift,
      },
      {
        label: 'page limit',
        request: etfBarRequestBody(),
        response: pageLimitDrift,
      },
      {
        label: 'record cardinality',
        request: { ...etfBarRequestBody(), page: { limit: 1 } },
        response: cardinalityDrift,
      },
      {
        label: 'cursor state',
        request: etfBarRequestBody(),
        response: cursorDrift,
      },
      {
        label: 'release quality',
        request: {
          ...etfBarRequestBody(),
          selection: { qualityStatuses: ['PASSED'] },
        },
        response: qualityDrift,
      },
      {
        label: 'selected dataVersion',
        request: {
          ...etfBarRequestBody(),
          selection: {
            qualityStatuses: ['PASSED', 'WARNED'],
            dataVersion: SELECTED_DATA_VERSION,
          },
        },
        response: etfAvailableResponse(),
      },
      {
        label: 'publication sources',
        request: etfBarRequestBody(),
        response: sourceDrift,
      },
    ];

    for (const testCase of cases) {
      const fetcher = vi
        .fn<typeof fetch>()
        .mockResolvedValue(downstreamResponse(testCase.response));
      const client = new MarketDataAccessClient(config, fetcher);

      await expect(
        client.query({ request: testCase.request, requestId: REQUEST_ID }),
        testCase.label,
      ).rejects.toMatchObject({
        status: HttpStatus.SERVICE_UNAVAILABLE,
        response: { code: 'dependency-unavailable' },
      });
    }
  });

  /** 即使下游省略 Content-Length，实际响应超过两 MiB 也必须在解析前安全拒绝。 */
  it('rejects an oversized streamed response without a declared length', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response('x'.repeat(2 * 1024 * 1024 + 1), {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
          'X-Request-Id': REQUEST_ID,
        },
      }),
    );
    const client = new MarketDataAccessClient(config, fetcher);

    await expect(
      client.query({ request: requestBody(), requestId: REQUEST_ID }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 非 JSON 成功响应不能进入 typed parser，避免代理或错误页被误当业务数据。 */
  it('rejects a success response with an unexpected media type', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response('{}', {
        status: 200,
        headers: {
          'Content-Type': 'text/plain',
          'X-Request-Id': REQUEST_ID,
        },
      }),
    );
    const client = new MarketDataAccessClient(config, fetcher);

    await expect(
      client.query({ request: requestBody(), requestId: REQUEST_ID }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 下游限流必须保留稳定状态与有界 Retry-After。 */
  it('preserves bounded retry advice from downstream rate limiting', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response('{}', { status: 429, headers: { 'Retry-After': '7' } }));
    const client = new MarketDataAccessClient(config, fetcher);

    await expect(
      client.query({ request: requestBody(), requestId: REQUEST_ID }),
    ).rejects.toMatchObject({
      status: HttpStatus.TOO_MANY_REQUESTS,
      response: { code: 'rate-limited', retryAfter: 7 },
    });
  });
});

/** 构造同步服务已实现的最小通用市场数据请求。 */
function requestBody(): Record<string, unknown> {
  return {
    dataset: { code: 'derivative.bar.1d.reported', schemaVersion: 1 },
    businessScope: 'CONTRACT',
    time: { dimension: 'TRADE_DATE', from: '2026-07-28', to: '2026-07-29' },
    visibility: { mode: 'CURRENT' },
    selection: { qualityStatuses: ['PASSED'] },
    fields: ['tradeDate'],
    filters: [
      {
        field: 'contractEntityRef',
        operator: 'EQ',
        values: ['00000000-0000-4000-8000-000000000199'],
      },
    ],
    sort: [{ field: 'tradeDate', direction: 'ASC' }],
  };
}

/** 构造合法 ETF v2 日线请求，业务值必须留在 typed-record values 中。 */
function etfBarRequestBody(): Record<string, unknown> {
  return {
    dataset: { code: 'fund.etf.bar.1d.reported', schemaVersion: 2 },
    businessScope: 'ETF',
    time: { dimension: 'TRADE_DATE', from: '2026-07-29', to: '2026-07-30' },
    visibility: { mode: 'CURRENT' },
    selection: { qualityStatuses: ['PASSED', 'WARNED'] },
    fields: ['tradeDate', 'etfEntityRef', 'close', 'adjustment'],
    filters: [{ field: 'etfEntityRef', operator: 'EQ', values: [ENTITY_REF] }],
    sort: [{ field: 'tradeDate', direction: 'ASC' }],
    page: { limit: 366 },
  };
}

/** 构造 envelope 合法但 record 被错误扁平化的下游响应。 */
function malformedEtfAvailableResponse(): Record<string, unknown> {
  return {
    ...etfAvailableResponse(),
    records: [
      {
        tradeDate: '2026-07-30',
        etfEntityRef: ENTITY_REF,
        close: '3.945',
        adjustment: 'UNADJUSTED',
        dataVersion: DATA_VERSION,
      },
    ],
  };
}

/** 构造可被 ETF v2 严格解析器接受的 AVAILABLE 日线响应，并仅允许测试显式扩展 release。 */
function etfAvailableResponse(
  releaseExtension: Record<string, unknown> = {},
): MutableEtfAvailableResponse {
  return {
    meta: {
      requestId: REQUEST_ID,
      contractVersion: '1.0.0',
      dataset: { code: 'fund.etf.bar.1d.reported', schemaVersion: 2 },
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
        ...releaseExtension,
      },
      visibility: { mode: 'CURRENT' },
      page: { limit: 366, hasMore: false, nextCursor: null },
      coverage: { from: '2026-07-29', to: '2026-07-30', pitCoverage: 'COMPLETE', gaps: [] },
      warnings: [],
      disclaimers: [],
    },
    records: [
      {
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
          close: '3.945',
          adjustment: 'UNADJUSTED',
        },
      },
    ],
  };
}

/** 把响应 fixture 包装成带一致关联标识和 publication 版本头的下游 HTTP 响应。 */
function downstreamResponse(body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'X-Data-Version': DATA_VERSION,
      'X-Request-Id': REQUEST_ID,
    },
  });
}
