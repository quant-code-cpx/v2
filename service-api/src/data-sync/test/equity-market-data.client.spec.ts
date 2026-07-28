import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { EquityMarketDataClient } from '../clients/equity-market-data.client.js';

/** 提供内部市场数据调用所需的最小配置。 */
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

const publication = {
  exchange: 'SSE',
  symbol: '600519',
  dataVersion: '00000000-0000-4000-8000-000000000001',
  publishedAt: '2026-07-28T00:00:00Z',
  qualityStatus: 'passed',
  stale: false,
} as const;

/** 覆盖四种市场数据请求、严格合同与安全错误映射。 */
describe('EquityMarketDataClient', () => {
  /** 验证周线查询直接映射内部周期、复权和日期参数。 */
  it('reads direct weekly bars with adjustment metadata', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        ...publication,
        period: '1w',
        adjustmentMode: 'qfq',
        adjustAsOf: '2026-07-28',
        factorVersion: '00000000-0000-4000-8000-000000000002',
        formulaVersion: 'cumulative-hfq-v1',
        items: [
          {
            periodEnd: '2026-07-24',
            open: '10.000000',
            high: '12.000000',
            low: '9.000000',
            close: '11.000000',
            volumeShares: '1000',
            amountCny: '10500',
            turnoverRate: '0.01',
            isFinal: true,
            revision: 1,
          },
        ],
        nextCursor: null,
      }),
    );
    const client = new EquityMarketDataClient(config, fetcher);

    const result = await client.listBars({
      exchange: 'SSE',
      symbol: '600519',
      period: '1w',
      start: '2026-01-01',
      end: '2026-07-28',
      adjust: 'qfq',
      adjustAsOf: '2026-07-28',
      cursor: 'next-page',
      limit: 500,
      ifNoneMatch: '"old"',
      requestId: 'req-bars',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) expect(result.body.items[0]?.open).toBe('10.000000');
    const [target, init] = fetcher.mock.calls[0] ?? [];
    const url = requestedUrl(target);
    expect(url.pathname).toBe('/internal/v1/equities/SSE/600519/bars');
    expect(Object.fromEntries(url.searchParams)).toMatchObject({
      period: '1w',
      adjust: 'qfq',
      adjustAsOf: '2026-07-28',
      cursor: 'next-page',
      limit: '500',
    });
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
      'If-None-Match': '"old"',
      'X-Request-Id': 'req-bars',
    });
  });

  /** 验证因子、事件与概况三个端点使用各自合同。 */
  it('reads factors actions and company profile', async () => {
    const responses = [
      jsonResponse({
        ...publication,
        factorVersion: publication.dataVersion,
        items: [
          {
            effectiveDate: '2026-01-01',
            cumulativeFactor: '2',
            revision: 1,
          },
        ],
        nextCursor: null,
      }),
      jsonResponse({
        ...publication,
        items: [
          {
            actionId: '00000000-0000-4000-8000-000000000003',
            revision: 1,
            reportPeriod: '2025-12-31',
            status: '实施',
            announcementDate: '2026-06-01',
            recordDate: null,
            exDate: '2026-06-30',
            cashDividendPer10: '10',
            bonusSharesPer10: null,
            transferSharesPer10: null,
          },
        ],
        nextCursor: null,
      }),
      jsonResponse({
        ...publication,
        revision: 1,
        profile: {
          companyName: '贵州茅台酒股份有限公司',
          englishName: null,
          industry: '白酒',
          legalRepresentative: null,
          establishedOn: '1999-11-20',
          website: null,
          email: null,
          phone: null,
          registeredAddress: '贵州',
          officeAddress: null,
          mainBusiness: '白酒',
          businessScope: null,
          summary: null,
        },
      }),
    ];
    const fetcher = vi
      .fn<typeof fetch>()
      .mockImplementation(() =>
        Promise.resolve(responses.shift() ?? new Response(null, { status: 500 })),
      );
    const client = new EquityMarketDataClient(config, fetcher);

    const factors = await client.listAdjustmentFactors({
      exchange: 'SSE',
      symbol: '600519',
      start: '2026-01-01',
      end: '2026-07-28',
      limit: 500,
      requestId: 'req-factors',
    });
    const actions = await client.listCorporateActions({
      exchange: 'SSE',
      symbol: '600519',
      limit: 100,
      requestId: 'req-actions',
    });
    const profile = await client.getCompanyProfile({
      exchange: 'SSE',
      symbol: '600519',
      requestId: 'req-profile',
    });

    expect(factors.status === 200 && factors.body.items[0]?.cumulativeFactor).toBe('2');
    expect(actions.status === 200 && actions.body.items[0]?.status).toBe('实施');
    expect(profile.status === 200 && profile.body.profile.industry).toBe('白酒');
    expect(requestedUrl(fetcher.mock.calls[2]?.[0]).pathname).toContain('company-profile');
  });

  /** 验证 304 保留 ETag，供公开 POST 映射为 204。 */
  it('preserves conditional not modified responses', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(null, { status: 304, headers: { ETag: '"current"' } }));
    const client = new EquityMarketDataClient(config, fetcher);

    const result = await client.getCompanyProfile({
      exchange: 'SSE',
      symbol: '600519',
      ifNoneMatch: '"current"',
      requestId: 'req-profile-cache',
    });

    expect(result).toEqual({ status: 304, etag: '"current"' });
  });

  /** 验证复权覆盖冲突、未知冲突和合同漂移都不会泄漏内部详情。 */
  it('maps allowlisted conflicts and invalid payloads safely', async () => {
    const adjustmentClient = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'adjustment-unavailable', detail: 'internal' }), {
          status: 409,
          headers: { 'Content-Type': 'application/problem+json' },
        }),
      ),
    );
    const invalidClient = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ...publication, secret: true })),
    );

    await expect(
      adjustmentClient.listBars({
        exchange: 'SSE',
        symbol: '600519',
        period: '1mo',
        start: '2026-01-01',
        end: '2026-07-28',
        adjust: 'hfq',
        limit: 500,
        requestId: 'req-conflict',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'adjustment-unavailable' },
    });
    await expect(
      invalidClient.getCompanyProfile({
        exchange: 'SSE',
        symbol: '600519',
        requestId: 'req-invalid',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 验证日期身份边界冲突使用公开稳定码，未知内部冲突仍安全降级。 */
  it('maps identity boundary and unknown conflicts without leaking downstream details', async () => {
    const identityClient = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'identity-boundary-conflict', detail: 'secret' }), {
          status: 409,
          headers: { 'Content-Type': 'application/problem+json' },
        }),
      ),
    );
    const unknownClient = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'unexpected-conflict', detail: 'secret' }), {
          status: 409,
          headers: { 'Content-Type': 'application/problem+json' },
        }),
      ),
    );
    const query = {
      exchange: 'SSE',
      symbol: '600519',
      start: '2026-01-01',
      end: '2026-07-28',
      limit: 100,
      requestId: 'req-identity-conflict',
    };

    await expect(identityClient.listCorporateActions(query)).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'identity-resolution-conflict' },
    });
    await expect(unknownClient.listCorporateActions(query)).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'snapshot-expired' },
    });
  });
});

/** 构造 JSON 成功响应。 */
function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ETag: '"v1"' },
  });
}

/** 将 fetch 目标统一解析为 URL。 */
function requestedUrl(target: Parameters<typeof fetch>[0] | undefined): URL {
  if (target instanceof URL) return target;
  if (typeof target === 'string') return new URL(target);
  if (target instanceof Request) return new URL(target.url);
  throw new Error('Expected fetch to receive a request target');
}
