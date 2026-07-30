import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import {
  STOCK_CONNECT_RESPONSE_BYTES,
  StockConnectClient,
} from '../clients/stock-connect.client.js';
import {
  STOCK_CONNECT_TEST_DATA_VERSION,
  stockConnectActiveSecurityPage,
  stockConnectOverviewResponse,
  stockConnectReadinessCrossLanguageVector,
  stockConnectReadinessResponse,
} from './stock-connect.test-data.js';

/** 提供沪深港通内部调用所需的最小配置。 */
const config = {
  dataSyncStockConnectBaseUrl: 'http://data-sync-api:8000',
  dataSyncStockConnectBearerToken: 'test-only-stock-connect-read-token-000000000000000000',
  dataSyncStockConnectTimeoutMs: 3_000,
  dataSyncStockConnectCircuitFailures: 5,
  dataSyncStockConnectCircuitWindowMs: 30_000,
  dataSyncStockConnectCircuitOpenMs: 30_000,
} as AppConfigService;

/** 测试中跳过真实退避，但保留重试次数和顺序。 */
function noWait(): Promise<void> {
  return Promise.resolve();
}

/** 返回确定随机值，使退避测试不受随机数影响。 */
function zeroRandom(): number {
  return 0;
}

/** 覆盖内部 POST、严格合同、版本头、重试、游标冲突和断路器。 */
describe('StockConnectClient', () => {
  /** 验证总览通过专用 POST 路由携带同一服务身份、请求标识与严格 JSON body。 */
  it('posts an overview query and validates version headers', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(stockConnectOverviewResponse(), 'req-overview'));
    const client = new StockConnectClient(config, fetcher);
    const query = {
      date: { mode: 'LATEST', exactDate: null },
      channels: ['SH_NORTHBOUND'],
      trendTradingDays: 20,
    };

    const result = await client.overview(query, 'req-overview');

    expect(result.dataVersion).toBe(STOCK_CONNECT_TEST_DATA_VERSION);
    expect(result.body.publication.dataVersion).toBe(STOCK_CONNECT_TEST_DATA_VERSION);
    const [target, init] = fetcher.mock.calls[0] ?? [];
    expect(requestedUrl(target).pathname).toBe('/internal/v1/stock-connect/overview/query');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(requestBodyText(init?.body))).toEqual(query);
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncStockConnectBearerToken}`,
      'Content-Type': 'application/json',
      'X-Request-Id': 'req-overview',
    });
  });

  /** 验证 readiness 使用独立 POST、稳定通道范围和正文规范 SHA-256 版本。 */
  it('posts readiness and verifies its canonical representation digest', async () => {
    const readiness = stockConnectReadinessResponse();
    const dataVersion = String(readiness.dataVersion);
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(readiness, 'req-readiness', dataVersion));
    const client = new StockConnectClient(config, fetcher);
    const request = {
      date: { mode: 'LATEST', exactDate: null },
      channels: ['SH_NORTHBOUND'],
    };

    const result = await client.readiness(request, 'req-readiness');

    expect(result.dataVersion).toBe(dataVersion);
    const [target, init] = fetcher.mock.calls[0] ?? [];
    expect(requestedUrl(target).pathname).toBe('/internal/v1/stock-connect/readiness/query');
    expect(JSON.parse(requestBodyText(init?.body))).toEqual(request);

    const mutated = stockConnectReadinessResponse();
    mutated.observedAt = '2026-07-29T10:21:00Z';
    const mutatedClient = new StockConnectClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          jsonResponse(mutated, 'req-readiness-drift', String(mutated.dataVersion)),
        ),
    );
    await expect(mutatedClient.readiness(request, 'req-readiness-drift')).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });
  });

  /** 验证 TypeScript 客户端与 Python 仓储对 Unicode、null 和数组使用同一规范哈希。 */
  it('matches the frozen cross-language readiness digest vector', async () => {
    const readiness = stockConnectReadinessCrossLanguageVector();
    const dataVersion = String(readiness.dataVersion);
    const client = new StockConnectClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse(readiness, 'req-readiness-vector', dataVersion)),
    );

    const result = await client.readiness(
      {
        date: { mode: 'EXACT', exactDate: '2026-07-30' },
        channels: ['SH_NORTHBOUND', 'SZ_SOUTHBOUND'],
      },
      'req-readiness-vector',
    );

    expect(result.dataVersion).toBe(
      'abe5d1926e56f9f60959b27141e450ad1a0f580437e59a8e737a1efe34276307',
    );
  });

  /** 验证首次 503 的响应体被取消后只重试一次，并成功读取第二个真实合同响应。 */
  it('cancels a transient response body before one safe retry', async () => {
    let cancelled = 0;
    /** 记录底层响应流被主动取消。 */
    function recordCancellation(): void {
      cancelled += 1;
    }
    const transient = new Response(new ReadableStream({ cancel: recordCancellation }), {
      status: 503,
    });
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(transient)
      .mockResolvedValueOnce(jsonResponse(stockConnectOverviewResponse(), 'req-retry'));
    const client = new StockConnectClient(config, fetcher, Date.now, noWait, zeroRandom);

    await client.overview(
      {
        date: { mode: 'LATEST', exactDate: null },
        channels: ['SH_NORTHBOUND'],
        trendTradingDays: 20,
      },
      'req-retry',
    );

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(cancelled).toBe(1);
  });

  /** 验证第二次 502/503 不再重试时也取消未消费正文，避免连接资源泄漏。 */
  it('cancels the final transient response body before mapping 503', async () => {
    let cancelled = 0;
    /** 记录两个独立失败响应流的取消次数。 */
    function recordCancellation(): void {
      cancelled += 1;
    }
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(transientResponse(recordCancellation))
      .mockResolvedValueOnce(transientResponse(recordCancellation));
    const client = new StockConnectClient(config, fetcher, Date.now, noWait, zeroRandom);

    await expect(
      client.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-final-transient',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(cancelled).toBe(2);
  });

  /** 验证旧 publication 游标保持公开 409，不会被错误改写为空页或 503。 */
  it('maps a stale active-security cursor to CURSOR_VERSION_MISMATCH', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(problemResponse(409, 'CURSOR_VERSION_MISMATCH', 'req-stale-cursor'));
    const client = new StockConnectClient(config, fetcher);

    await expect(
      client.activeSecurities(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          ranking: 'SOURCE_ACTIVE',
          parentPublicationDataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
          cursor: 'old-publication-cursor',
          limit: 20,
        },
        'req-stale-cursor',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'CURSOR_VERSION_MISMATCH' },
    });
  });

  /** 验证父 publication 更新时保持公开 409，阻止跨版本统计与榜单拼接。 */
  it('maps a changed parent publication to PARENT_PUBLICATION_MISMATCH', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(problemResponse(409, 'PARENT_PUBLICATION_MISMATCH', 'req-parent-version'));
    const client = new StockConnectClient(config, fetcher);

    await expect(
      client.activeSecurities(
        {
          date: { mode: 'LATEST', exactDate: null },
          channel: 'SH_NORTHBOUND',
          ranking: 'SOURCE_ACTIVE',
          parentPublicationDataVersion: 'previous-parent-version',
          cursor: null,
          limit: 20,
        },
        'req-parent-version',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'PARENT_PUBLICATION_MISMATCH' },
    });
  });

  /** 验证未知字段、缺失版本头和错误请求标识都作为上游合同破坏失败关闭。 */
  it('fails closed on response contract drift', async () => {
    const withUnknownField = { ...stockConnectOverviewResponse(), providerPath: '/restricted' };
    const driftClient = new StockConnectClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(withUnknownField, 'req-drift')),
    );
    await expect(
      driftClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-drift',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });

    const badVersionClient = new StockConnectClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          jsonResponse(stockConnectOverviewResponse(), 'req-version', 'another-version'),
        ),
    );
    await expect(
      badVersionClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-version',
      ),
    ).rejects.toMatchObject({ status: HttpStatus.SERVICE_UNAVAILABLE });

    const badRequestIdClient = new StockConnectClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse(stockConnectOverviewResponse(), 'another-request')),
    );
    await expect(
      badRequestIdClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-id',
      ),
    ).rejects.toMatchObject({ status: HttpStatus.SERVICE_UNAVAILABLE });
  });

  /** 验证总览声明长度等于预算时可读取，超过一个字节则在解析前失败关闭。 */
  it('enforces the overview response byte budget at the exact boundary', async () => {
    const serializedBody = JSON.stringify(stockConnectOverviewResponse());
    const bodyBytes = new TextEncoder().encode(serializedBody).byteLength;
    const paddingBytes = STOCK_CONNECT_RESPONSE_BYTES.overview - bodyBytes;
    const exactBoundary = new Response(`${serializedBody}${' '.repeat(paddingBytes)}`, {
      status: 200,
      headers: {
        'Content-Length': String(STOCK_CONNECT_RESPONSE_BYTES.overview),
        'Content-Type': 'application/json',
        'X-Request-Id': 'req-exact-budget',
        'X-Data-Version': STOCK_CONNECT_TEST_DATA_VERSION,
      },
    });
    const acceptedClient = new StockConnectClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(exactBoundary),
    );

    await expect(
      acceptedClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-exact-budget',
      ),
    ).resolves.toMatchObject({ dataVersion: STOCK_CONNECT_TEST_DATA_VERSION });

    const exceededBoundary = jsonResponse(stockConnectOverviewResponse(), 'req-over-budget');
    exceededBoundary.headers.set(
      'Content-Length',
      String(STOCK_CONNECT_RESPONSE_BYTES.overview + 1),
    );
    const rejectedClient = new StockConnectClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(exceededBoundary),
    );

    await expect(
      rejectedClient.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-over-budget',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });
  });

  /** 验证无长度头的分块正文一旦超限便立即取消流，不会先无界聚合。 */
  it('cancels an oversized chunked response without a content length', async () => {
    let cancelled = 0;
    /** 预置恰好预算和额外一个字节，且保持流开启以观察客户端主动取消。 */
    function enqueueOversizedBody(controller: ReadableStreamDefaultController<Uint8Array>): void {
      controller.enqueue(new Uint8Array(STOCK_CONNECT_RESPONSE_BYTES.overview));
      controller.enqueue(new Uint8Array([0]));
    }
    /** 记录客户端在额外字节到达时主动取消底层流。 */
    function recordCancellation(): void {
      cancelled += 1;
    }
    const response = successStreamResponse(
      new ReadableStream({
        start: enqueueOversizedBody,
        cancel: recordCancellation,
      }),
      'req-chunked-overflow',
    );
    const client = new StockConnectClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(response),
    );

    await expect(
      client.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-chunked-overflow',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });
    expect(cancelled).toBe(1);
  });

  /** 验证 Content-Length 只接受纯十进制数字，指数或符号形式均失败关闭。 */
  it('rejects a non-decimal content length', async () => {
    const response = jsonResponse(stockConnectOverviewResponse(), 'req-invalid-length');
    response.headers.set('Content-Length', '1e3');
    const client = new StockConnectClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(response),
    );

    await expect(
      client.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-invalid-length',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });
  });

  /** 验证非法 UTF-8 不能被替换字符静默修复并进入 JSON 解析。 */
  it('rejects invalid UTF-8 bytes', async () => {
    const response = successStreamResponse(
      new ReadableStream({
        /** 产生一个包含非法连续字节的短正文。 */
        start(controller): void {
          controller.enqueue(new Uint8Array([0xc3, 0x28]));
          controller.close();
        },
      }),
      'req-invalid-utf8',
    );
    const client = new StockConnectClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(response),
    );

    await expect(
      client.overview(
        {
          date: { mode: 'LATEST', exactDate: null },
          channels: ['SH_NORTHBOUND'],
          trendTradingDays: 20,
        },
        'req-invalid-utf8',
      ),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });
  });

  /** 验证连续逻辑故障达到阈值后快速失败，不继续冲击下游。 */
  it('opens the circuit after the configured consecutive failure threshold', async () => {
    const circuitConfig = {
      ...config,
      dataSyncStockConnectCircuitFailures: 1,
    } as AppConfigService;
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 503 }));
    const client = new StockConnectClient(circuitConfig, fetcher, Date.now, noWait, zeroRandom);
    const query = {
      date: { mode: 'LATEST', exactDate: null },
      channels: ['SH_NORTHBOUND'],
      trendTradingDays: 20,
    };

    await expect(client.overview(query, 'req-first')).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
    });
    await expect(client.overview(query, 'req-circuit')).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  /** 验证符合最终字段约束的活跃证券页能够原样通过严格合同。 */
  it('accepts rankingRank separately from sourceRank', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(stockConnectActiveSecurityPage(), 'req-active'));
    const client = new StockConnectClient(config, fetcher);

    const result = await client.activeSecurities(
      {
        date: { mode: 'LATEST', exactDate: null },
        channel: 'SH_NORTHBOUND',
        ranking: 'SOURCE_ACTIVE',
        parentPublicationDataVersion: STOCK_CONNECT_TEST_DATA_VERSION,
        cursor: null,
        limit: 20,
      },
      'req-active',
    );

    expect(result.body.items[0]).toMatchObject({ rankingRank: 1, sourceRank: 1 });
  });
});

/** 构造带严格关联头和 publication 版本的 JSON 成功响应。 */
function jsonResponse(
  body: Record<string, unknown>,
  requestId: string,
  dataVersion = STOCK_CONNECT_TEST_DATA_VERSION,
): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'X-Request-Id': requestId,
      'X-Data-Version': dataVersion,
    },
  });
}

/** 构造符合内部 RFC 9457 合同的错误响应。 */
function problemResponse(status: number, code: string, requestId: string): Response {
  return new Response(
    JSON.stringify({
      type: `https://data-sync.local/problems/${code}`,
      title: 'Conflict',
      status,
      detail: 'Request conflicts with an immutable publication',
      instance: '/internal/v1/stock-connect/active-securities/query',
      code,
      requestId,
    }),
    {
      status,
      headers: { 'Content-Type': 'application/problem+json' },
    },
  );
}

/** 构造需要客户端主动释放正文的临时网关失败响应。 */
function transientResponse(onCancel: () => void): Response {
  return new Response(new ReadableStream({ cancel: onCancel }), { status: 503 });
}

/** 构造没有 Content-Length、但带完整成功关联头的流式响应。 */
function successStreamResponse(body: ReadableStream<Uint8Array>, requestId: string): Response {
  return new Response(body, {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'X-Request-Id': requestId,
      'X-Data-Version': STOCK_CONNECT_TEST_DATA_VERSION,
    },
  });
}

/** 将 Fetch 请求目标统一解析成 URL。 */
function requestedUrl(target: Parameters<typeof fetch>[0] | undefined): URL {
  if (target instanceof URL) return target;
  if (typeof target === 'string') return new URL(target);
  if (target instanceof Request) return new URL(target.url);
  throw new Error('Expected fetch to receive a request target');
}

/** 将测试 Fetch 请求体收窄为 client 固定发送的 JSON 字符串。 */
function requestBodyText(body: unknown): string {
  if (typeof body === 'string') return body;
  throw new Error('Expected fetch to receive a JSON string body');
}
