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

/** 提供所有 reference 资源共有的最小已发布版本字段。 */
const referencePublication = {
  exchange: 'SSE',
  symbol: '600519',
  dataVersion: '00000000-0000-4000-8000-000000000001',
  publishedAt: '2026-07-28T00:00:00Z',
  qualityStatus: 'passed',
  stale: false,
} as const;

/** 提供行情窗口额外需要的可用性与来源观测字段。 */
const publication = {
  ...referencePublication,
  availability: 'AVAILABLE',
  observedAt: null,
  reasonCode: null,
} as const;

/** 提供一条可公开审计、但不含服务内证券身份的精确窗口谱系。 */
const barCoverageLineage = {
  coverageVersion: '00000000-0000-4000-8000-000000000004',
  publicationKind: 'DATA',
  sourceBatchId: '00000000-0000-4000-8000-000000000005',
} as const;

/** 提供因子 publication 即使无记录也必须返回的冻结来源投影。 */
const adjustmentFactorPublicationSource = {
  sourceBatchId: '00000000-0000-4000-8000-000000000006',
  providerId: 'akshare-sina-adjustment-factor',
  upstreamSource: 'sina-hfq-factor',
  adapterVersion: 'akshare-1.18.81-v1',
} as const;

/** 覆盖四种市场数据请求、严格合同与安全错误映射。 */
describe('EquityMarketDataClient', () => {
  /** 验证周线查询直接映射内部周期、复权和日期参数。 */
  it('reads direct weekly bars with adjustment metadata', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        ...publication,
        ...barCoverageLineage,
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
      dataVersion: publication.dataVersion,
      factorDataVersion: '00000000-0000-4000-8000-000000000002',
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
    expect(result).toMatchObject({ etag: '"v1"', dataVersion: publication.dataVersion });
    if (result.status === 200) {
      expect(result.body.items[0]?.open).toBe('10.000000');
      expect(result.body).toMatchObject(barCoverageLineage);
      expect(result.body).not.toHaveProperty('securityId');
      expect(result.body).not.toHaveProperty('identifierVersionId');
    }
    const [target, init] = fetcher.mock.calls[0] ?? [];
    const url = requestedUrl(target);
    expect(url.pathname).toBe('/internal/v1/equities/SSE/600519/bars');
    expect(Object.fromEntries(Array.from(url.searchParams.entries()))).toMatchObject({
      dataVersion: publication.dataVersion,
      factorDataVersion: '00000000-0000-4000-8000-000000000002',
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

  /** 验证任一叶子资源忽略请求锁定的 publication 时统一失败关闭。 */
  it('rejects successful responses from a different requested publication', async () => {
    const differentDataVersion = '00000000-0000-4000-8000-000000000099';
    const responses = [
      jsonResponse(
        publishedBarPage({
          dataVersion: differentDataVersion,
          period: '1d',
          adjustmentMode: 'none',
          adjustAsOf: null,
          factorVersion: null,
          formulaVersion: null,
        }),
        differentDataVersion,
      ),
      jsonResponse(
        {
          ...referencePublication,
          dataVersion: differentDataVersion,
          factorVersion: differentDataVersion,
          source: adjustmentFactorPublicationSource,
          items: [],
          nextCursor: null,
        },
        differentDataVersion,
      ),
      jsonResponse(
        {
          ...referencePublication,
          dataVersion: differentDataVersion,
          items: [],
          nextCursor: null,
        },
        differentDataVersion,
      ),
      jsonResponse(
        {
          ...referencePublication,
          dataVersion: differentDataVersion,
          identityAsOf: '2026-07-28',
          revision: 1,
          sourceBatchId: '00000000-0000-4000-8000-000000000006',
          profile: companyProfile(),
        },
        differentDataVersion,
      ),
    ];
    const client = new EquityMarketDataClient(
      config,
      vi
        .fn<typeof fetch>()
        .mockImplementation(() =>
          Promise.resolve(responses.shift() ?? new Response(null, { status: 500 })),
        ),
    );

    await expect(
      client.listBars({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion: publication.dataVersion,
        period: '1d',
        start: '2026-07-28',
        end: '2026-07-28',
        adjust: 'none',
        limit: 100,
        requestId: 'req-bars-version-mismatch',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listAdjustmentFactors({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion: publication.dataVersion,
        end: '2026-07-28',
        limit: 100,
        requestId: 'req-factors-version-mismatch',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listCorporateActions({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion: publication.dataVersion,
        limit: 100,
        requestId: 'req-actions-version-mismatch',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.getCompanyProfile({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion: publication.dataVersion,
        requestId: 'req-profile-version-mismatch',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 验证行情响应不能替换调用方指定的复权版本、模式或锚点。 */
  it('rejects mismatched adjustment bindings', async () => {
    const requestedFactorVersion = '00000000-0000-4000-8000-000000000002';
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse(
          publishedBarPage({
            period: '1d',
            adjustmentMode: 'qfq',
            adjustAsOf: '2026-07-28',
            factorVersion: '00000000-0000-4000-8000-000000000003',
            formulaVersion: 'cumulative-hfq-v1',
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          publishedBarPage({
            period: '1d',
            adjustmentMode: 'hfq',
            adjustAsOf: null,
            factorVersion: requestedFactorVersion,
            formulaVersion: 'cumulative-hfq-v1',
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          publishedBarPage({
            period: '1d',
            adjustmentMode: 'qfq',
            adjustAsOf: '2026-07-27',
            factorVersion: requestedFactorVersion,
            formulaVersion: 'cumulative-hfq-v1',
          }),
        ),
      );
    const client = new EquityMarketDataClient(config, fetcher);
    const request = {
      exchange: 'SSE',
      symbol: '600519',
      dataVersion: publication.dataVersion,
      factorDataVersion: requestedFactorVersion,
      period: '1d',
      start: '2026-07-01',
      end: '2026-07-28',
      adjust: 'qfq',
      limit: 100,
    } as const;

    await expect(
      client.listBars({ ...request, requestId: 'req-factor-version-mismatch' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listBars({ ...request, requestId: 'req-adjustment-mode-mismatch' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listBars({ ...request, requestId: 'req-adjust-as-of-mismatch' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 验证后复权省略锚点时仍绑定内部合同规定的查询结束日。 */
  it('accepts hfq with the implicit end adjustment anchor', async () => {
    const requestedFactorVersion = '00000000-0000-4000-8000-000000000002';
    const client = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse(
          publishedBarPage({
            period: '1d',
            adjustmentMode: 'hfq',
            adjustAsOf: '2026-07-28',
            factorVersion: requestedFactorVersion,
            formulaVersion: 'cumulative-hfq-v1',
          }),
        ),
      ),
    );

    const result = await client.listBars({
      exchange: 'SSE',
      symbol: '600519',
      dataVersion: publication.dataVersion,
      factorDataVersion: requestedFactorVersion,
      period: '1d',
      start: '2026-07-01',
      end: '2026-07-28',
      adjust: 'hfq',
      limit: 100,
      requestId: 'req-hfq-implicit-anchor',
    });

    expect(result.status).toBe(200);
  });

  /** 验证已证实零记录窗口保留精确覆盖谱系，不退化为旧的无版本空页。 */
  it('accepts a zero-record coverage publication with immutable lineage', async () => {
    const client = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          ...publication,
          ...barCoverageLineage,
          publicationKind: 'ZERO_RECORD_COVERAGE',
          period: '1d',
          adjustmentMode: 'none',
          adjustAsOf: null,
          factorVersion: null,
          formulaVersion: null,
          items: [],
          nextCursor: null,
        }),
      ),
    );

    const result = await client.listBars({
      exchange: 'SSE',
      symbol: '600519',
      dataVersion: publication.dataVersion,
      period: '1d',
      start: '2026-07-28',
      end: '2026-07-28',
      adjust: 'none',
      limit: 100,
      requestId: 'req-zero-coverage',
    });

    expect(result.status).toBe(200);
    expect(result).toMatchObject({ etag: '"v1"', dataVersion: publication.dataVersion });
    if (result.status === 200) {
      expect(result.body.publicationKind).toBe('ZERO_RECORD_COVERAGE');
      expect(result.body.coverageVersion).toBe(barCoverageLineage.coverageVersion);
      expect(result.body.sourceBatchId).toBe(barCoverageLineage.sourceBatchId);
      expect(result.body.items).toEqual([]);
      expect(result.body.availability).toBe('AVAILABLE');
    }
  });

  /** 验证伪造谱系、错误零记录形状或服务内身份 UUID 都不能穿过公开 K 线合同。 */
  it('rejects invalid coverage lineage, invalid zero-record shapes and internal identity fields', async () => {
    const validPage = {
      ...publication,
      ...barCoverageLineage,
      period: '1d',
      adjustmentMode: 'none',
      adjustAsOf: null,
      factorVersion: null,
      formulaVersion: null,
      items: [
        {
          periodEnd: '2026-07-28',
          open: '10',
          high: '11',
          low: '9',
          close: '10',
          volumeShares: '1000',
          amountCny: '10000',
          turnoverRate: null,
          isFinal: true,
          revision: 1,
        },
      ],
      nextCursor: null,
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ ...validPage, coverageVersion: 'not-a-uuid' }))
      .mockResolvedValueOnce(jsonResponse({ ...validPage, sourceBatchId: 'not-a-uuid' }))
      .mockResolvedValueOnce(jsonResponse({ ...validPage, items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({ ...validPage, publicationKind: 'ZERO_RECORD_COVERAGE' }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ...validPage, securityId: '00000000-0000-4000-8000-000000000099' }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ...validPage, identifierVersionId: '00000000-0000-4000-8000-000000000098' }),
      );
    const client = new EquityMarketDataClient(config, fetcher);
    const request = {
      exchange: 'SSE',
      symbol: '600519',
      dataVersion: publication.dataVersion,
      period: '1d',
      start: '2026-07-28',
      end: '2026-07-28',
      adjust: 'none',
      limit: 100,
    } as const;

    await expect(
      client.listBars({ ...request, requestId: 'req-invalid-coverage' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listBars({ ...request, requestId: 'req-invalid-batch' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listBars({ ...request, requestId: 'req-empty-data' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listBars({ ...request, requestId: 'req-nonempty-zero' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listBars({ ...request, requestId: 'req-security-id' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    await expect(
      client.listBars({ ...request, requestId: 'req-identifier-version-id' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 验证因子、事件与概况三个端点使用各自合同。 */
  it('reads factors actions and company profile', async () => {
    const responses = [
      jsonResponse({
        ...referencePublication,
        factorVersion: publication.dataVersion,
        source: adjustmentFactorPublicationSource,
        items: [
          {
            effectiveDate: '2026-01-01',
            cumulativeFactor: '2',
            revision: 1,
            sourceBatchId: '00000000-0000-4000-8000-000000000006',
          },
        ],
        nextCursor: null,
      }),
      jsonResponse({
        ...referencePublication,
        items: [
          {
            actionId: '00000000-0000-4000-8000-000000000003',
            revision: 1,
            sourceBatchId: '00000000-0000-4000-8000-000000000007',
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
        ...referencePublication,
        identityAsOf: '2026-07-28',
        revision: 1,
        sourceBatchId: '00000000-0000-4000-8000-000000000008',
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
      dataVersion: publication.dataVersion,
      start: '2026-01-01',
      end: '2026-07-28',
      limit: 500,
      requestId: 'req-factors',
    });
    const actions = await client.listCorporateActions({
      exchange: 'SSE',
      symbol: '600519',
      dataVersion: publication.dataVersion,
      limit: 100,
      requestId: 'req-actions',
    });
    const profile = await client.getCompanyProfile({
      exchange: 'SSE',
      symbol: '600519',
      dataVersion: publication.dataVersion,
      asOf: '2026-07-28',
      requestId: 'req-profile',
    });

    expect(factors.status === 200 && factors.body.items[0]?.cumulativeFactor).toBe('2');
    expect(actions.status === 200 && actions.body.items[0]?.status).toBe('实施');
    expect(profile.status === 200 && profile.body.profile.industry).toBe('白酒');
    expect(factors.status === 200 && factors.body.items[0]?.sourceBatchId).toBe(
      '00000000-0000-4000-8000-000000000006',
    );
    expect(factors.status === 200 && factors.body.source).toEqual(adjustmentFactorPublicationSource);
    expect(actions.status === 200 && actions.body.items[0]?.sourceBatchId).toBe(
      '00000000-0000-4000-8000-000000000007',
    );
    expect(profile.status === 200 && profile.body.sourceBatchId).toBe(
      '00000000-0000-4000-8000-000000000008',
    );
    const profileUrl = requestedUrl(fetcher.mock.calls[2]?.[0]);
    expect(profileUrl.pathname).toContain('company-profile');
    expect(profileUrl.searchParams.get('asOf')).toBe('2026-07-28');
  });

  /** 验证缺少技术 publication 的成功空页失败关闭，页面应以 data-status 表达无 publication。 */
  it('rejects an unversioned empty bar page', async () => {
    const client = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          exchange: 'SSE',
          symbol: '600519',
          period: '1d',
          adjustmentMode: 'none',
          adjustAsOf: null,
          factorVersion: null,
          formulaVersion: null,
          dataVersion: null,
          publishedAt: null,
          availability: 'SOURCE_UNAVAILABLE',
          observedAt: '2026-07-29T00:00:00Z',
          reasonCode: 'unavailable',
          qualityStatus: null,
          stale: false,
          items: [],
          nextCursor: null,
        }),
      ),
    );

    await expect(
      client.listBars({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion: publication.dataVersion,
        period: '1d',
        start: '2026-07-01',
        end: '2026-07-29',
        adjust: 'none',
        limit: 100,
        requestId: 'req-empty',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 验证 304 保留 ETag 与 publication，供公开 POST 映射为可复验 204。 */
  it('preserves conditional not modified responses', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, {
        status: 304,
        headers: { ETag: '"current"', 'X-Data-Version': publication.dataVersion },
      }),
    );
    const client = new EquityMarketDataClient(config, fetcher);

    const result = await client.getCompanyProfile({
      exchange: 'SSE',
      symbol: '600519',
      dataVersion: publication.dataVersion,
      asOf: '2026-07-28',
      ifNoneMatch: '"current"',
      requestId: 'req-profile-cache',
    });

    expect(result).toEqual({
      status: 304,
      etag: '"current"',
      dataVersion: publication.dataVersion,
    });
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
    const coverageClient = new EquityMarketDataClient(
      config,
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'coverage-unavailable', detail: 'internal' }), {
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
        dataVersion: publication.dataVersion,
        factorDataVersion: '00000000-0000-4000-8000-000000000002',
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
      coverageClient.listBars({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion: publication.dataVersion,
        period: '1d',
        start: '2026-07-28',
        end: '2026-07-28',
        adjust: 'none',
        limit: 100,
        requestId: 'req-coverage-conflict',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'coverage-unavailable' },
    });
    await expect(
      invalidClient.getCompanyProfile({
        exchange: 'SSE',
        symbol: '600519',
        dataVersion: publication.dataVersion,
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
      dataVersion: publication.dataVersion,
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
function jsonResponse(body: unknown, dataVersion: string = publication.dataVersion): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      ETag: '"v1"',
      'X-Data-Version': dataVersion,
    },
  });
}

/** 构造一页满足公开 K 线合同的最小事实记录。 */
function publishedBarPage(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    ...publication,
    ...barCoverageLineage,
    items: [
      {
        periodEnd: '2026-07-28',
        open: '10',
        high: '11',
        low: '9',
        close: '10',
        volumeShares: '1000',
        amountCny: '10000',
        turnoverRate: null,
        isFinal: true,
        revision: 1,
      },
    ],
    nextCursor: null,
    ...overrides,
  };
}

/** 构造满足公司概况合同的最小公开字段集。 */
function companyProfile(): Record<string, string | null> {
  return {
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
  };
}

/** 将 fetch 目标统一解析为 URL。 */
function requestedUrl(target: Parameters<typeof fetch>[0] | undefined): URL {
  if (target instanceof URL) return target;
  if (typeof target === 'string') return new URL(target);
  if (target instanceof Request) return new URL(target.url);
  throw new Error('Expected fetch to receive a request target');
}
