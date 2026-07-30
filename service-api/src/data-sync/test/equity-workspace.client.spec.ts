import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { EquityWorkspaceClient } from '../clients/equity-workspace.client.js';

const DATA_VERSION = '00000000-0000-4000-8000-000000000001';
const EVENT_WINDOW = { start: '2026-07-01', end: '2026-07-30' } as const;
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token',
  dataSyncInternalRequestTimeoutMs: 2_000,
} as AppConfigService;

/** 覆盖股票中心真实内部 POST、防腐投影、条件读取与错误映射。 */
describe('EquityWorkspaceClient', () => {
  /** search 应发送严格内部 body，并裁剪生命周期命名和 release dataset。 */
  it('posts discovery query and projects a partial publication', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(internalSearchResponse(), 'req-search'));
    const client = new EquityWorkspaceClient(config, fetcher);

    const result = await client.search({
      body: {
        exchanges: ['SSE'],
        lifecycleStatuses: ['LISTED'],
        columns: ['symbol', 'listingStatus'],
        limit: 50,
      },
      ifNoneMatch: '"older"',
      requestId: 'req-search',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body.release).toMatchObject({ completeness: 'PARTIAL' });
      expect(result.body.release).not.toHaveProperty('dataset');
      expect(result.body.records[0]?.statuses.listingStatus).toBe('LISTED');
    }
    const [target, init] = fetcher.mock.calls[0] ?? [];
    expect(requestedUrl(target).pathname).toBe('/internal/v1/equity-discovery/query');
    expect(init).toMatchObject({
      method: 'POST',
      headers: {
        Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
        'If-None-Match': '"older"',
        'X-Request-Id': 'req-search',
      },
    });
    if (typeof init?.body !== 'string') throw new TypeError('Expected JSON request body');
    expect(JSON.parse(init.body) as unknown).toMatchObject({
      lifecycleStatuses: ['LISTED'],
      limit: 50,
    });
  });

  /** 事件成功响应应裁剪内部 eventId，并生成不可逆的公开 eventRef。 */
  it('posts event query and projects public event references', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(internalEventResponse(), 'req-events'));
    const client = new EquityWorkspaceClient(config, fetcher);

    const result = await client.searchEvents({
      exchange: 'SSE',
      symbol: '600519',
      body: { families: ['EARNINGS_FORECAST'], ...EVENT_WINDOW, limit: 50 },
      requestId: 'req-events',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body.events[0]?.eventRef).toMatch(/^evt_[A-Za-z0-9_-]{43}$/);
      expect(result.body.events[0]).not.toHaveProperty('eventId');
      expect(result.body.release).not.toHaveProperty('dataset');
    }
    const [target] = fetcher.mock.calls[0] ?? [];
    expect(requestedUrl(target).pathname).toBe('/internal/v1/equities/SSE/600519/events/query');
  });

  /** data-status 成功响应必须携带独立数据状态和强版本头。 */
  it('reads a strict data-status response with its version metadata', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(dataStatusResponse(), 'req-status-current'));
    const client = new EquityWorkspaceClient(config, fetcher);

    const result = await client.getDataStatus({
      exchange: 'SSE',
      symbol: '600519',
      body: { families: ['BARS_1D'] },
      requestId: 'req-status-current',
    });

    expect(result).toMatchObject({
      status: 200,
      etag: '"discovery-v1"',
      dataVersion: DATA_VERSION,
    });
    if (result.status === 200) {
      expect(result.body.datasets[0]).toMatchObject({
        family: 'BARS_1D',
        availability: 'AVAILABLE',
        freshness: 'FRESH',
        retryable: false,
      });
    }
  });

  /** data-status 的内部 304 应保留强 ETag 与数据版本供公开 POST 返回 204。 */
  it('preserves conditional data-status metadata', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, {
        status: 304,
        headers: {
          ETag: '"status-v1"',
          'X-Data-Version': DATA_VERSION,
          'X-Request-Id': 'req-status',
        },
      }),
    );
    const client = new EquityWorkspaceClient(config, fetcher);

    const result = await client.getDataStatus({
      exchange: 'SSE',
      symbol: '600519',
      body: {},
      ifNoneMatch: '"status-v1"',
      requestId: 'req-status',
    });

    expect(result).toEqual({
      status: 304,
      etag: '"status-v1"',
      dataVersion: DATA_VERSION,
    });
  });

  /** 无 discovery publication 应返回明确 UNAVAILABLE，不伪造成零条筛选结果。 */
  it('maps publication-unavailable to the public unavailable envelope', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'publication-unavailable', detail: 'internal' }), {
        status: 503,
        headers: {
          'Content-Type': 'application/problem+json',
          'X-Request-Id': 'req-empty',
        },
      }),
    );
    const client = new EquityWorkspaceClient(config, fetcher);

    const result = await client.search({ body: { limit: 100 }, requestId: 'req-empty' });

    expect(result.status).toBe(200);
    expect(result.status === 200 && result.body).toMatchObject({
      availability: 'UNAVAILABLE',
      reasonCode: 'NO_PUBLICATION',
      release: null,
      records: [],
      page: { limit: 100 },
    });
    if (result.status === 200) {
      expect(result.body.capabilities.columns).not.toContain('moneyFlowNetAmount');
      expect(result.body.capabilities.columns).not.toContain('moneyFlowNetRatio');
      expect(result.body.capabilities.sortFields).not.toContain('moneyFlowNetAmount');
    }
  });

  /** 事件无 publication 返回明确状态，但 data-status 保留可重试 503。 */
  it('distinguishes unavailable event and data-status publications', async () => {
    const eventClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(problemResponse(503, 'publication-unavailable', 'req-events-empty')),
    );
    const statusClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(problemResponse(503, 'publication-unavailable', 'req-status-empty')),
    );

    const eventResult = await eventClient.searchEvents({
      exchange: 'SSE',
      symbol: '600519',
      body: { ...EVENT_WINDOW, limit: 25 },
      requestId: 'req-events-empty',
    });

    expect(eventResult.status === 200 && eventResult.body).toMatchObject({
      availability: 'UNAVAILABLE',
      reasonCode: 'NO_PUBLICATION',
      release: null,
      events: [],
      page: { limit: 25 },
    });
    await expect(
      statusClient.getDataStatus({
        exchange: 'SSE',
        symbol: '600519',
        body: {},
        requestId: 'req-status-empty',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'publication-unavailable' },
    });
  });

  /** 合同漂移和未知冲突必须失败关闭，不能伪装成可重试的快照变化。 */
  it('fails closed on contract drift and unknown conflicts', async () => {
    const driftClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          jsonResponse({ ...internalSearchResponse(), internalSecret: true }, 'req-drift'),
        ),
    );
    const conflictClient = new EquityWorkspaceClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'internal-conflict', detail: 'secret' }), {
          status: 409,
          headers: { 'X-Request-Id': 'req-conflict' },
        }),
      ),
    );

    await expect(
      driftClient.search({ body: { limit: 50 }, requestId: 'req-drift' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      conflictClient.searchEvents({
        exchange: 'SSE',
        symbol: '600519',
        body: { ...EVENT_WINDOW, limit: 50 },
        requestId: 'req-conflict',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 只把冻结身份冲突与快照冲突映射为对应的公开稳定问题码。 */
  it('maps allowlisted identity and snapshot conflicts precisely', async () => {
    const responses = [
      { internal: 'identity-resolution-conflict', public: 'identity-resolution-conflict' },
      { internal: 'identity-incomplete', public: 'identity-resolution-conflict' },
      { internal: 'snapshot-expired', public: 'snapshot-expired' },
    ] as const;

    for (const conflict of responses) {
      const requestId = `req-${conflict.internal}`;
      const client = new EquityWorkspaceClient(
        config,
        vi.fn<typeof fetch>().mockResolvedValue(
          new Response(JSON.stringify({ code: conflict.internal, detail: 'secret' }), {
            status: 409,
            headers: {
              'Content-Type': 'application/problem+json',
              'X-Request-Id': requestId,
            },
          }),
        ),
      );

      await expect(
        client.searchEvents({
          exchange: 'SSE',
          symbol: '600519',
          body: { ...EVENT_WINDOW, limit: 50 },
          requestId,
        }),
      ).rejects.toMatchObject({
        status: HttpStatus.CONFLICT,
        response: { code: conflict.public },
      });
    }
  });

  /** 下游 400、404 与 429 只能映射为冻结公开问题码。 */
  it('maps whitelisted upstream failures without leaking downstream details', async () => {
    const invalidClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(problemResponse(400, 'internal-validation', 'req-invalid')),
    );
    const missingClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(problemResponse(404, 'internal-not-found', 'req-missing')),
    );
    const limitedClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          problemResponse(429, 'internal-limit', 'req-limited', { 'Retry-After': '17' }),
        ),
    );

    await expect(
      invalidClient.search({ body: { limit: 50 }, requestId: 'req-invalid' }),
    ).rejects.toMatchObject({
      status: HttpStatus.BAD_REQUEST,
      response: { code: 'validation-error' },
    });
    await expect(
      missingClient.searchEvents({
        exchange: 'SSE',
        symbol: '600519',
        body: { ...EVENT_WINDOW, limit: 50 },
        requestId: 'req-missing',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.NOT_FOUND,
      response: { code: 'equity-not-found' },
    });
    await expect(
      limitedClient.getDataStatus({
        exchange: 'SSE',
        symbol: '600519',
        body: {},
        requestId: 'req-limited',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.TOO_MANY_REQUESTS,
      response: { code: 'rate-limited', retryAfter: 17 },
    });
  });

  /** 响应关联标识或 publication 版本不一致时必须失败关闭。 */
  it('rejects mismatched correlation and data-version headers', async () => {
    const correlationClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse(internalSearchResponse(), 'another-request')),
    );
    const versionClient = new EquityWorkspaceClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          jsonResponse(
            internalSearchResponse(),
            'req-version-mismatch',
            '00000000-0000-4000-8000-000000000002',
          ),
        ),
    );

    await expect(
      correlationClient.search({ body: { limit: 50 }, requestId: 'req-correlation' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      versionClient.search({ body: { limit: 50 }, requestId: 'req-version-mismatch' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 连接失败只重试一次，硬超时不重试以保持交互 deadline。 */
  it('retries one connection failure but never retries a timeout', async () => {
    const retryFetcher = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError('connection reset'))
      .mockResolvedValueOnce(jsonResponse(internalSearchResponse(), 'req-retry'));
    const timeout = new Error('deadline exceeded');
    timeout.name = 'TimeoutError';
    const timeoutFetcher = vi.fn<typeof fetch>().mockRejectedValue(timeout);

    await expect(
      new EquityWorkspaceClient(config, retryFetcher).search({
        body: { limit: 50 },
        requestId: 'req-retry',
      }),
    ).resolves.toMatchObject({ status: 200 });
    await expect(
      new EquityWorkspaceClient(config, timeoutFetcher).search({
        body: { limit: 50 },
        requestId: 'req-timeout',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    expect(retryFetcher).toHaveBeenCalledTimes(2);
    expect(timeoutFetcher).toHaveBeenCalledTimes(1);
  });
});

/** 构造带强缓存头和关联标识的真实 JSON 响应。 */
function jsonResponse(
  body: unknown,
  requestId: string,
  dataVersion: string = DATA_VERSION,
): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      ETag: '"discovery-v1"',
      'X-Data-Version': dataVersion,
      'X-Request-Id': requestId,
    },
  });
}

/** 构造不暴露 detail 的内部 Problem 响应。 */
function problemResponse(
  status: number,
  code: string,
  requestId: string,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify({ code, detail: 'internal-only' }), {
    status,
    headers: {
      'Content-Type': 'application/problem+json',
      'X-Request-Id': requestId,
      ...headers,
    },
  });
}

/** 构造 strict Zod 可消费的内部 discovery publication。 */
function internalSearchResponse() {
  return {
    availability: 'AVAILABLE',
    reasonCode: null,
    release: {
      dataset: 'equity.discovery.eod',
      dataVersion: DATA_VERSION,
      publishedAt: '2026-07-30T08:00:00Z',
      effectiveAsOf: '2026-07-29',
      knowledgeCutoff: '2026-07-30T07:30:00Z',
      qualityStatus: 'warning',
      completeness: 'PARTIAL',
    },
    components: [],
    capabilities: {
      sortFields: ['symbol'],
      columns: ['symbol', 'listingStatus'],
      maxLimit: 100,
    },
    records: [
      {
        identity: {
          exchange: 'SSE',
          symbol: '600519',
          name: '贵州茅台',
          identityAsOf: '2026-07-29',
        },
        statuses: { lifecycleStatus: 'LISTED', tradingStatus: 'TRADED' },
        market: { close: '1418.88', currency: 'CNY' },
        capitalization: { totalShares: '1256197800', currency: 'CNY' },
        valuation: { peTtm: '22.5' },
        moneyFlow: { netAmountCny: '-120000000' },
        memberships: [],
      },
    ],
    page: { nextCursor: null, limit: 50 },
  };
}

/** 构造 strict Zod 可消费的内部证券事件 publication。 */
function internalEventResponse() {
  return {
    availability: 'AVAILABLE',
    reasonCode: null,
    release: {
      dataset: 'equity.events',
      dataVersion: DATA_VERSION,
      publishedAt: '2026-07-30T08:00:00Z',
      effectiveAsOf: '2026-07-29',
      knowledgeCutoff: '2026-07-30T07:30:00Z',
      qualityStatus: 'passed',
    },
    events: [
      {
        eventId: 'source:EARNINGS_FORECAST:600519:2026-07-29',
        family: 'EARNINGS_FORECAST',
        kind: 'FORECAST',
        announcedOn: '2026-07-29',
        reportPeriod: '2026-06-30',
        dataVersion: DATA_VERSION,
        facts: [{ code: 'NET_PROFIT_CHANGE', valueLow: '-10.5', valueHigh: '20' }],
      },
    ],
    page: { nextCursor: null, limit: 50 },
  };
}

/** 构造 strict Zod 可消费的详情数据状态响应。 */
function dataStatusResponse() {
  return {
    identity: {
      exchange: 'SSE',
      symbol: '600519',
      name: '贵州茅台',
      identityAsOf: '2026-07-29',
    },
    datasets: [
      {
        family: 'BARS_1D',
        dataset: 'equity.bar.1d',
        availability: 'AVAILABLE',
        freshness: 'FRESH',
        dataVersion: DATA_VERSION,
        publishedAt: '2026-07-30T08:00:00Z',
        effectiveAsOf: '2026-07-29',
        knowledgeCutoff: '2026-07-30T07:30:00Z',
        sourceLabel: 'tushare',
        methodology: { code: 'daily-bar', version: '1' },
        reasonCode: null,
        retryable: false,
      },
    ],
  };
}

/** 将 fetch 输入统一解析为 URL。 */
function requestedUrl(target: Parameters<typeof fetch>[0] | undefined): URL {
  if (target instanceof URL) return target;
  if (typeof target === 'string') return new URL(target);
  if (target instanceof Request) return new URL(target.url);
  throw new Error('Expected fetch target');
}
