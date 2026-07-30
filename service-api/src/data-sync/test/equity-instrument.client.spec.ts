import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { EquityInstrumentClient } from '../clients/equity-instrument.client.js';

/** 提供只包含内部调用所需字段的测试配置。 */
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

/** 模拟 0009 内部合同中的双时态证券条目。 */
const internalEquity = {
  instrumentId: '00000000-0000-4000-8000-000000000001',
  identifier: {
    exchange: 'SSE',
    symbol: '600000',
    effectiveFrom: '1999-11-10',
    effectiveTo: null,
    datePrecision: 'OFFICIAL_DATE',
    knownFrom: '2026-07-01T00:00:00Z',
    observedAt: '2026-07-01T00:00:00Z',
  },
  name: {
    value: '浦发银行',
    effectiveFrom: '1999-11-10',
    effectiveTo: null,
    datePrecision: 'OFFICIAL_DATE',
    knownFrom: '2026-07-01T00:00:00Z',
    observedAt: '2026-07-01T00:00:00Z',
  },
  listing: {
    status: 'LISTED',
    listedOn: '1999-11-10',
    delistedOn: null,
    effectiveFrom: '1999-11-10',
    effectiveTo: null,
    datePrecision: 'OFFICIAL_DATE',
    knownFrom: '2026-07-01T00:00:00Z',
    observedAt: '2026-07-01T00:00:00Z',
  },
} as const;

/** 模拟稳定发布携带的可复验元数据。 */
const publication = {
  dataVersion: '00000000-0000-4000-8000-000000000002',
  publishedAt: '2026-07-01T00:00:00Z',
  effectiveAsOf: '2026-06-30',
  knowledgeCutoff: '2026-07-01T00:00:00Z',
} as const;

/** 覆盖内部合同校验、公开投影、请求关联和安全错误映射。 */
describe('EquityInstrumentClient', () => {
  /** 验证目录条件读取完整透传筛选参数，同时移除内部证券 UUID。 */
  it('forwards list filters and strips internal instrument identifiers', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [internalEquity],
          nextCursor: 'next-page',
          ...publication,
          publicationScope: 'SSE',
        }),
        {
          status: 200,
          headers: {
            ETag: '"equities-v1"',
            'Content-Type': 'application/json',
            'X-Data-Version': publication.dataVersion,
          },
        },
      ),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    const result = await client.listEquities({
      exchange: 'SSE',
      statuses: ['LISTED', 'SUSPENDED'],
      query: '浦发',
      asOf: '2026-06-30',
      knownAt: '2026-07-01T00:00:00Z',
      cursor: 'current-page',
      limit: 50,
      ifNoneMatch: '"equities-v0"',
      requestId: 'req-list-equities',
    });

    expect(result).toMatchObject({ status: 200, etag: '"equities-v1"' });
    if (result.status === 200) {
      expect(result.body.items[0]).not.toHaveProperty('instrumentId');
      expect(result.body.items[0]).toMatchObject({
        identifier: { exchange: 'SSE', symbol: '600000' },
        name: { value: '浦发银行' },
      });
    }

    const [target, init] = fetcher.mock.calls[0] ?? [];
    const url = requestedUrl(target);
    expect(url.pathname).toBe('/internal/v1/equities');
    expect(url.searchParams.getAll('status')).toEqual(['LISTED', 'SUSPENDED']);
    expect(Object.fromEntries(Array.from(url.searchParams.entries()))).toMatchObject({
      exchange: 'SSE',
      query: '浦发',
      asOf: '2026-06-30',
      knownAt: '2026-07-01T00:00:00Z',
      cursor: 'current-page',
      limit: '50',
    });
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
      'If-None-Match': '"equities-v0"',
      'X-Request-Id': 'req-list-equities',
    });
  });

  /** 验证券详情路径和时间切片正确映射，且详情同样不泄漏内部 UUID。 */
  it('projects a temporal equity detail into the public contract', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ...internalEquity, ...publication }), {
        status: 200,
        headers: {
          ETag: '"equity-v1"',
          'Content-Type': 'application/json',
          'X-Data-Version': publication.dataVersion,
        },
      }),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    const result = await client.getEquity({
      exchange: 'SSE',
      symbol: '600000',
      asOf: '2026-06-30',
      knownAt: '2026-07-01T00:00:00Z',
      requestId: 'req-equity-detail',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) expect(result.body).not.toHaveProperty('instrumentId');
    const [target] = fetcher.mock.calls[0] ?? [];
    expect(requestedUrl(target).toString()).toContain(
      '/internal/v1/equities/SSE/600000?asOf=2026-06-30&knownAt=2026-07-01T00%3A00%3A00Z',
    );
  });

  /** 验证历史读取保留双时态区间并裁剪页级内部证券 UUID。 */
  it('projects listing status history and forwards its stable cursor', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          instrumentId: internalEquity.instrumentId,
          exchange: 'SSE',
          symbol: '600000',
          items: [
            {
              status: 'LISTED',
              effectiveFrom: '1999-11-10',
              effectiveTo: null,
              effectiveDatePrecision: 'OFFICIAL_DATE',
              knownFrom: '2026-07-01T00:00:00Z',
              knownTo: null,
              observedAt: '2026-07-01T00:00:00Z',
            },
          ],
          nextCursor: null,
          dataVersion: publication.dataVersion,
          publishedAt: publication.publishedAt,
          knowledgeCutoff: publication.knowledgeCutoff,
        }),
        {
          status: 200,
          headers: {
            ETag: '"history-v1"',
            'Content-Type': 'application/json',
            'X-Data-Version': publication.dataVersion,
          },
        },
      ),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    const result = await client.listListingStatusHistory({
      exchange: 'SSE',
      symbol: '600000',
      asOf: '2026-06-30',
      effectiveFrom: '1999-01-01',
      effectiveTo: '2026-07-01',
      knownAt: '2026-07-01T00:00:00Z',
      cursor: 'history-page',
      limit: 25,
      requestId: 'req-equity-history',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body).not.toHaveProperty('instrumentId');
      expect(result.body.items[0]).toMatchObject({
        status: 'LISTED',
        effectiveDatePrecision: 'OFFICIAL_DATE',
      });
    }
    const [target] = fetcher.mock.calls[0] ?? [];
    const url = requestedUrl(target);
    expect(url.pathname).toBe('/internal/v1/equities/SSE/600000/listing-status-history');
    expect(url.searchParams.get('cursor')).toBe('history-page');
  });

  /** 验证未知下游冲突码不会被泄漏，并按带游标历史读取语义降级为快照过期。 */
  it('allowlists downstream conflict codes', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'private-storage-conflict', detail: 'secret' }), {
        status: 409,
        headers: { 'Content-Type': 'application/problem+json' },
      }),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    await expect(
      client.listListingStatusHistory({
        exchange: 'SSE',
        symbol: '600000',
        cursor: 'stale-page',
        limit: 50,
        requestId: 'req-conflict',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'snapshot-expired' },
    });
  });

  /** 验证端点不支持的合法下游冲突码也会按该端点合同安全回退。 */
  it('does not expose a conflict code unsupported by the public endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'identity-resolution-conflict' }), {
        status: 409,
        headers: { 'Content-Type': 'application/problem+json' },
      }),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    await expect(
      client.listEquities({ limit: 50, requestId: 'req-list-conflict' }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'snapshot-expired' },
    });
  });

  /** 验证无身份 publication 使用稳定公开码，详情页可区别于普通依赖故障。 */
  it('preserves the publication unavailable state without downstream details', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ code: 'publication-unavailable', detail: 'internal release table' }),
        {
          status: 503,
          headers: { 'Content-Type': 'application/problem+json' },
        },
      ),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    await expect(
      client.getEquity({
        exchange: 'SSE',
        symbol: '600000',
        requestId: 'req-no-publication',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'publication-unavailable' },
    });
  });

  /** 验证条件命中同时保留强 ETag 与发布版本，供公开 POST 安全映射为 204。 */
  it('preserves the publication version on a conditional hit', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, {
        status: 304,
        headers: { ETag: '"equities-v1"', 'X-Data-Version': publication.dataVersion },
      }),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    await expect(
      client.listEquities({
        limit: 50,
        ifNoneMatch: '"equities-v1"',
        requestId: 'req-list-cache',
      }),
    ).resolves.toEqual({
      status: 304,
      etag: '"equities-v1"',
      dataVersion: publication.dataVersion,
    });
  });

  /** 验证响应头与正文 publication 不一致时失败关闭，不能污染浏览器条件缓存。 */
  it('rejects a mismatched publication header', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ...internalEquity, ...publication }), {
        status: 200,
        headers: {
          ETag: '"equity-v1"',
          'Content-Type': 'application/json',
          'X-Data-Version': '00000000-0000-4000-8000-000000000099',
        },
      }),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    await expect(
      client.getEquity({
        exchange: 'SSE',
        symbol: '600000',
        requestId: 'req-version-mismatch',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 验证内部响应字段漂移被隔离为公开 503，而不是原样返回浏览器。 */
  it('maps an invalid downstream payload to dependency unavailable', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [{ ...internalEquity, unexpectedInternalField: true }],
          nextCursor: null,
          ...publication,
          publicationScope: 'SSE',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const client = new EquityInstrumentClient(config, fetcher);

    await expect(
      client.listEquities({ limit: 50, requestId: 'req-invalid-response' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });
});

/** 将 fetch 测试调用的目标统一解析为可断言的 URL。 */
function requestedUrl(target: Parameters<typeof fetch>[0] | undefined): URL {
  if (target instanceof URL) return target;
  if (typeof target === 'string') return new URL(target);
  if (target instanceof Request) return new URL(target.url);
  throw new Error('Expected fetch to receive a request target');
}
