import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import type { AppConfigService } from '../../config/app-config.service.js';
import { MarketOverviewClient } from '../clients/market-overview.client.js';
import type { SwIndustryConstituentPage } from '../contracts/market-overview.contract.js';
import {
  createMarketIndexBarPageFixture,
  createMarketOverviewFixture,
} from './market-overview.fixtures.js';

/** 构造市场 client 测试需要的最小已校验配置。 */
function createConfig(): AppConfigService {
  return {
    dataSyncInternalBaseUrl: 'http://data-sync.test',
    dataSyncInternalBearerToken: 'test-service-token-with-at-least-thirty-two-characters',
    dataSyncInternalRequestTimeoutMs: 1_500,
  } as AppConfigService;
}

/** 构造带关联标识、强 ETag 和 publication 版本头的成功内部响应。 */
function successResponse(body: { dataVersion: string }, requestId: string): Response {
  return new Response(JSON.stringify(body), {
    status: HttpStatus.OK,
    headers: {
      'content-type': 'application/json',
      etag: '"market-version-1"',
      'x-data-version': body.dataVersion,
      'x-request-id': requestId,
    },
  });
}

/** 构造指定快照日的申万正式成分页，供请求响应身份绑定测试复用。 */
function swConstituentPage(snapshotDate: string): SwIndustryConstituentPage {
  const source = createMarketOverviewFixture().indices[0]?.source;
  if (source === undefined) throw new Error('market source fixture is unavailable');
  return {
    dataVersion: '00000000-0000-4000-8000-000000000019',
    snapshotDate,
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

/** 断言一个市场读取 Promise 以指定公开状态失败。 */
async function expectPublicFailure(promise: Promise<unknown>, status: number): Promise<void> {
  try {
    await promise;
    throw new Error('expected market request to fail');
  } catch (error) {
    expect(error).toBeInstanceOf(PublicProblemException);
    expect((error as PublicProblemException).getStatus()).toBe(status);
  }
}

describe('MarketOverviewClient', () => {
  /** 验证完整包通过内部 GET、身份头和严格 publication 头返回。 */
  it('reads one atomic overview bundle through the versioned internal GET', async () => {
    const fixture = createMarketOverviewFixture();
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(successResponse(fixture, 'market/overview:request-1'));
    const client = new MarketOverviewClient(createConfig(), fetcher);

    const result = await client.getOverview({
      asOf: '2026-07-30',
      ifNoneMatch: '"previous"',
      requestId: 'market/overview:request-1',
    });

    expect(result.status).toBe(200);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const firstCall = fetcher.mock.calls[0];
    expect(firstCall?.[0]).toBeInstanceOf(URL);
    expect((firstCall?.[0] as URL).href).toBe(
      'http://data-sync.test/internal/v1/market/overview-bundles/2026-07-30',
    );
    const options = firstCall?.[1];
    expect(options?.method).toBe('GET');
    const headers = new Headers(options?.headers);
    expect(headers.get('authorization')).toBe(
      'Bearer test-service-token-with-at-least-thirty-two-characters',
    );
    expect(headers.get('if-none-match')).toBe('"previous"');
    expect(headers.get('x-request-id')).toBe('market/overview:request-1');
  });

  /** 验证内部 304 必须携带可复验 ETag 与 dataVersion。 */
  it('preserves a valid internal not-modified result', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, {
        status: HttpStatus.NOT_MODIFIED,
        headers: {
          etag: '"market-version-1"',
          'x-data-version': '00000000-0000-4000-8000-000000000001',
          'x-request-id': 'request-2',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expect(
      client.getOverview({ ifNoneMatch: '"market-version-1"', requestId: 'request-2' }),
    ).resolves.toEqual({
      status: 304,
      etag: '"market-version-1"',
      dataVersion: '00000000-0000-4000-8000-000000000001',
    });
  });

  /** 验证动态市场状态可使用独立实体 ETag，同时保持同一个 EOD dataVersion。 */
  it('accepts a status-aware entity tag independent from the EOD data version', async () => {
    const fixture = createMarketOverviewFixture();
    const body = {
      ...fixture,
      status: {
        ...fixture.status,
        marketState: 'trading' as const,
        marketStateAsOf: '2026-07-30T10:01:00+08:00',
      },
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: HttpStatus.OK,
        headers: {
          etag: '"market-status-2026-07-30T10:01+08:00"',
          'x-data-version': fixture.dataVersion,
          'x-request-id': 'request-status-etag',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expect(client.getOverview({ requestId: 'request-status-etag' })).resolves.toMatchObject({
      status: 200,
      etag: '"market-status-2026-07-30T10:01+08:00"',
      dataVersion: fixture.dataVersion,
      body: { status: { marketState: 'trading' } },
    });
  });

  /** 验证缺少四指数的下游合同漂移不会作为 200 暴露。 */
  it('maps response contract drift to dataset unavailable', async () => {
    const fixture = createMarketOverviewFixture();
    const invalid = { ...fixture, indices: fixture.indices.slice(0, 3) };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(successResponse(invalid, 'request-3'));
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.getOverview({ requestId: 'request-3' }),
      HttpStatus.SERVICE_UNAVAILABLE,
    );
  });

  /** 验证精确日期资源即使 schema 合法也不能返回另一交易日的完整包。 */
  it('rejects a schema-valid overview whose resource date does not match the request', async () => {
    const fixture = createMarketOverviewFixture();
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(successResponse(fixture, 'request-wrong-date'));
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.getOverview({ asOf: '2026-07-29', requestId: 'request-wrong-date' }),
      HttpStatus.SERVICE_UNAVAILABLE,
    );
  });

  /** 验证精确日期没有 publication 时保留公开 404，而不是回退其他日期。 */
  it('maps a missing exact publication to public 404', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'publication-not-found' }), {
        status: HttpStatus.NOT_FOUND,
        headers: {
          'content-type': 'application/problem+json',
          'x-request-id': 'request-4',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.getOverview({ asOf: '2026-07-29', requestId: 'request-4' }),
      HttpStatus.NOT_FOUND,
    );
  });

  /** 验证资金流排行把方向、日期和页大小编码到冻结内部资源路径。 */
  it('uses the dedicated internal resource for equity money-flow rankings', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'publication-not-found' }), {
        status: HttpStatus.NOT_FOUND,
        headers: {
          'content-type': 'application/problem+json',
          'x-request-id': 'request-money-flow',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.listEquityMoneyFlowRankings({
        asOf: '2026-07-30',
        direction: 'inflow',
        limit: 20,
        requestId: 'request-money-flow',
      }),
      HttpStatus.NOT_FOUND,
    );
    const requestUrl = fetcher.mock.calls[0]?.[0];
    expect(requestUrl).toBeInstanceOf(URL);
    expect((requestUrl as URL).href).toBe(
      'http://data-sync.test/internal/v1/market/money-flow/equity-rankings?direction=inflow&limit=20&asOf=2026-07-30',
    );
  });

  /** 验证 7500 个 lineage UUID 加 1000 条 K 线仍能在 2 MiB client 预算内严格解析。 */
  it('accepts the maximum index lineage and bar page within the response budget', async () => {
    const fixture = createMarketIndexBarPageFixture(7_500, 1_000);
    expect(new TextEncoder().encode(JSON.stringify(fixture)).byteLength).toBeLessThanOrEqual(
      2 * 1_024 * 1_024,
    );
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(successResponse(fixture, 'request-index-capacity'));
    const client = new MarketOverviewClient(createConfig(), fetcher);

    const result = await client.listIndexBars({
      indexId: 'sse-composite',
      period: '1d',
      start: '2006-01-01',
      end: '2026-07-30',
      limit: 1_000,
      requestId: 'request-index-capacity',
    });
    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body.volumeUnit).toBe('lot');
      expect(result.body.inputDataVersions).toHaveLength(7_500);
      expect(result.body.items).toHaveLength(1_000);
    }
  });

  /** 验证板块资金流摘要读取独立 publication，不复用价格强弱排行。 */
  it('uses the dedicated internal resource for sector money-flow rankings', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'publication-not-found' }), {
        status: HttpStatus.NOT_FOUND,
        headers: {
          'content-type': 'application/problem+json',
          'x-request-id': 'request-sector-money-flow',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.listSectorMoneyFlowRankings({
        scheme: 'eastmoney.industry',
        asOf: '2026-07-30',
        order: 'desc',
        limit: 20,
        requestId: 'request-sector-money-flow',
      }),
      HttpStatus.NOT_FOUND,
    );
    const requestUrl = fetcher.mock.calls[0]?.[0];
    expect(requestUrl).toBeInstanceOf(URL);
    expect((requestUrl as URL).href).toBe(
      'http://data-sync.test/internal/v1/market/sectors/money-flow-rankings?scheme=eastmoney.industry&order=desc&limit=20&asOf=2026-07-30',
    );
  });

  /** 验证申万精确节点估值使用独立资源，而不是扫描整个层级分页。 */
  it('uses the exact SW industry valuation resource', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'resource-not-found' }), {
        status: HttpStatus.NOT_FOUND,
        headers: {
          'content-type': 'application/problem+json',
          'x-request-id': 'request-sw-valuation',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.getSwIndustryValuation({
        code: '801010.SI',
        asOf: '2026-07-30',
        requestId: 'request-sw-valuation',
      }),
      HttpStatus.NOT_FOUND,
    );
    const requestUrl = fetcher.mock.calls[0]?.[0];
    expect(requestUrl).toBeInstanceOf(URL);
    expect((requestUrl as URL).href).toBe(
      'http://data-sync.test/internal/v1/market/industries/sw/801010.SI/valuation?asOf=2026-07-30',
    );
  });

  /** 验证精确 asOf 成分请求拒绝同步端回退到其他日期的合法响应。 */
  it('rejects an SW constituent response whose snapshot date differs from exact asOf', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        successResponse(swConstituentPage('2026-07-29'), 'request-sw-constituents-date'),
      );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.listSwIndustryConstituents({
        code: '801010.SI',
        asOf: '2026-07-30',
        limit: 100,
        requestId: 'request-sw-constituents-date',
      }),
      HttpStatus.SERVICE_UNAVAILABLE,
    );
  });

  /** 验证冻结 publication 变化时游标冲突保持公开 409。 */
  it('maps a cursor mismatch to public 409', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'cursor-mismatch' }), {
        status: HttpStatus.CONFLICT,
        headers: {
          'content-type': 'application/problem+json',
          'x-request-id': 'request-conflict',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.listEquityRankings({
        metric: 'amountCny',
        order: 'desc',
        cursor: 'stale-cursor',
        limit: 20,
        requestId: 'request-conflict',
      }),
      HttpStatus.CONFLICT,
    );
  });

  /** 验证必要 publication 组件不可用时保留明确 424，而不是返回部分 200。 */
  it('maps a missing required component to public 424', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'required-component-unavailable' }), {
        status: HttpStatus.FAILED_DEPENDENCY,
        headers: {
          'content-type': 'application/problem+json',
          'x-request-id': 'request-required-component',
        },
      }),
    );
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.getOverview({ requestId: 'request-required-component' }),
      HttpStatus.FAILED_DEPENDENCY,
    );
  });

  /** 验证下游关联标识串线时立即失败，绝不把其他请求的响应暴露给当前调用。 */
  it('rejects a response whose request id does not match the outbound request', async () => {
    const fixture = createMarketOverviewFixture();
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(successResponse(fixture, 'different-request'));
    const client = new MarketOverviewClient(createConfig(), fetcher);

    await expectPublicFailure(
      client.getOverview({ requestId: 'expected-request' }),
      HttpStatus.SERVICE_UNAVAILABLE,
    );
  });

  /** 验证连续五次完整逻辑网络失败后断路，避免继续冲击同步读端。 */
  it('opens the read circuit after five logical dependency failures', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error('network unavailable'));
    const client = new MarketOverviewClient(createConfig(), fetcher);

    for (let attempt = 0; attempt < 5; attempt += 1) {
      await expectPublicFailure(
        client.getOverview({ requestId: `request-circuit-${attempt}` }),
        HttpStatus.SERVICE_UNAVAILABLE,
      );
    }
    expect(fetcher).toHaveBeenCalledTimes(10);

    await expectPublicFailure(
      client.getOverview({ requestId: 'request-circuit-open' }),
      HttpStatus.SERVICE_UNAVAILABLE,
    );
    expect(fetcher).toHaveBeenCalledTimes(10);
  });
});
