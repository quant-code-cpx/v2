import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { SectorMarketDataClient } from '../clients/sector-market-data.client.js';

const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

/** 覆盖下游合同投影、认证边界与安全错误映射。 */
describe('SectorMarketDataClient', () => {
  /** 验证非法关联标识不会进入内部请求头或触发下游调用。 */
  it('rejects an unsafe request id before calling data sync', async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = new SectorMarketDataClient(config, fetcher);

    await expect(
      client.listSectors({
        scheme: 'eastmoney.industry',
        limit: 100,
        requestId: 'unsafe request id',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  /** 验证目录读取携带内部凭据和 ETag，但移除不应公开的稳定 UUID。 */
  it('forwards conditional headers and strips internal sector identifiers', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              sectorId: '00000000-0000-4000-8000-000000000001',
              scheme: 'eastmoney.industry',
              code: 'BK0475',
              name: '证券',
              dataVersion: '00000000-0000-4000-8000-000000000002',
              publishedAt: '2026-07-01T00:00:00Z',
            },
          ],
          nextCursor: null,
          dataVersion: '00000000-0000-4000-8000-000000000002',
          publishedAt: '2026-07-01T00:00:00Z',
        }),
        {
          status: 200,
          headers: {
            ETag: '"catalog-v1"',
            'Content-Type': 'application/json',
            'X-Data-Version': '00000000-0000-4000-8000-000000000002',
            'X-Request-Id': 'sector/catalog:test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    const result = await client.listSectors({
      scheme: 'eastmoney.industry',
      limit: 100,
      ifNoneMatch: '"catalog-v0"',
      requestId: 'sector/catalog:test',
    });

    expect(result).toMatchObject({ status: 200, etag: '"catalog-v1"' });
    if (result.status === 200) {
      expect(result.body.items[0]).not.toHaveProperty('sectorId');
      expect(result.body.items[0]).toMatchObject({ code: 'BK0475', name: '证券' });
    }
    const [, init] = fetcher.mock.calls[0] ?? [];
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
      'If-None-Match': '"catalog-v0"',
      'X-Request-Id': 'sector/catalog:test',
    });
  });

  /** 验证下游快照冲突不会泄漏响应体，而是变成公开稳定问题码。 */
  it('maps upstream snapshot conflicts to the public problem contract', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, {
        status: 409,
        headers: { 'X-Request-Id': 'sector-bars-test' },
      }),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    await expect(
      client.listBars({
        scheme: 'eastmoney.industry',
        code: 'BK0475',
        period: '1w',
        start: '2026-06-01',
        end: '2026-06-30',
        limit: 100,
        requestId: 'sector-bars-test',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'snapshot-expired' },
    });
  });

  /** 验证来源未报告量额时保留 null，且只接受同步期已终结的正式周期。 */
  it('preserves truthful null volume and amount on final materialized bars', async () => {
    const dataVersion = '00000000-0000-4000-8000-000000000041';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          sector: {
            sectorId: '00000000-0000-4000-8000-000000000042',
            scheme: 'eastmoney.industry',
            code: 'BK0475',
            name: '证券',
            dataVersion: '00000000-0000-4000-8000-000000000043',
            publishedAt: '2026-07-30T10:00:00Z',
          },
          period: '1mo',
          dataVersion,
          publishedAt: '2026-07-30T10:01:00Z',
          items: [
            {
              periodEnd: '2026-07-30',
              open: '1000',
              high: '1020',
              low: '990',
              close: '1010',
              volumeValue: null,
              volumeUnit: 'provider_native',
              amountCny: null,
              amplitudePercent: '3',
              changePercent: '1',
              changeAmount: '10',
              turnoverPercent: null,
              isFinal: true,
            },
          ],
          nextCursor: null,
        }),
        {
          status: 200,
          headers: {
            ETag: '"bar-v1"',
            'X-Data-Version': dataVersion,
            'X-Request-Id': 'sector-bar-null-test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    const result = await client.listBars({
      scheme: 'eastmoney.industry',
      code: 'BK0475',
      period: '1mo',
      start: '2026-07-01',
      end: '2026-07-31',
      limit: 100,
      requestId: 'sector-bar-null-test',
    });

    expect(result.status).toBe(200);
    if (result.status === 200) {
      expect(result.body.items[0]).toMatchObject({
        volumeValue: null,
        amountCny: null,
        isFinal: true,
      });
    }
  });

  /** 验证 EOD 排行页严格校验快照合同、透传排序参数并删除内部板块 UUID。 */
  it('projects EOD rankings without internal identifiers', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          scheme: 'eastmoney.industry',
          tradeDate: '2026-07-27',
          sourceCutoffAt: '2026-07-27T08:15:00Z',
          observedAt: '2026-07-27T08:20:00Z',
          finality: 'post_close_observation',
          qualityStatus: 'passed',
          dataVersion: '00000000-0000-4000-8000-000000000031',
          publishedAt: '2026-07-27T08:21:00Z',
          inputDataVersions: [
            '00000000-0000-4000-8000-000000000033',
            '00000000-0000-4000-8000-000000000034',
          ],
          sort: 'changePercent',
          order: 'desc',
          items: [
            {
              sectorId: '00000000-0000-4000-8000-000000000032',
              scheme: 'eastmoney.industry',
              code: 'BK0475',
              name: '证券',
              latestValue: '1000',
              latestValueUnit: 'provider_native',
              changeValue: '10',
              changePercent: '1',
              marketValue: '1000000',
              marketValueUnit: 'CNY',
              turnoverPercent: '3',
              advancers: 10,
              decliners: 3,
              leaderName: '示例证券',
              leaderChangePercent: '5',
              rank: 1,
              position: 1,
            },
          ],
          nextCursor: null,
        }),
        {
          status: 200,
          headers: {
            ETag: '"eod-v1"',
            'X-Data-Version': '00000000-0000-4000-8000-000000000031',
            'X-Request-Id': 'sector-eod-test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    const result = await client.listEodSnapshots({
      scheme: 'eastmoney.industry',
      asOf: '2026-07-27',
      sort: 'changePercent',
      order: 'desc',
      limit: 100,
      requestId: 'sector-eod-test',
    });

    expect(result).toMatchObject({ status: 200, etag: '"eod-v1"' });
    if (result.status === 200) {
      expect(result.body.items[0]).not.toHaveProperty('sectorId');
      expect(result.body).toMatchObject({
        finality: 'post_close_observation',
        tradeDate: '2026-07-27',
        dataVersion: '00000000-0000-4000-8000-000000000031',
        inputDataVersions: [
          '00000000-0000-4000-8000-000000000033',
          '00000000-0000-4000-8000-000000000034',
        ],
      });
    }
    const eodUrl = fetcher.mock.calls[0]?.[0];
    expect(eodUrl).toBeInstanceOf(URL);
    if (!(eodUrl instanceof URL)) throw new Error('expected URL request');
    expect(eodUrl.pathname).toBe('/internal/v1/sectors/eod-snapshots');
    expect(eodUrl.searchParams.get('asOf')).toBe('2026-07-27');
  });

  /** 验证单板块 EOD 同样以报价与领先股组件的 composite 版本绑定响应头和正文。 */
  it('accepts a composite EOD resource whose header and body versions agree', async () => {
    const dataVersion = '00000000-0000-4000-8000-000000000035';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          scheme: 'eastmoney.industry',
          tradeDate: '2026-07-27',
          sourceCutoffAt: '2026-07-27T08:15:00Z',
          observedAt: '2026-07-27T08:20:00Z',
          finality: 'post_close_observation',
          qualityStatus: 'passed',
          dataVersion,
          publishedAt: '2026-07-27T08:21:00Z',
          inputDataVersions: [
            '00000000-0000-4000-8000-000000000033',
            '00000000-0000-4000-8000-000000000034',
          ],
          sectorId: '00000000-0000-4000-8000-000000000032',
          code: 'BK0475',
          name: '证券',
          latestValue: '1000',
          latestValueUnit: 'provider_native',
          changeValue: '10',
          changePercent: '1',
          marketValue: '1000000',
          marketValueUnit: 'CNY',
          turnoverPercent: '3',
          advancers: 10,
          decliners: 3,
          leaderName: '示例证券',
          leaderChangePercent: '5',
        }),
        {
          status: 200,
          headers: {
            ETag: '"eod-resource-v1"',
            'X-Data-Version': dataVersion,
            'X-Request-Id': 'sector-eod-resource-test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    const result = await client.getEodSnapshot({
      scheme: 'eastmoney.industry',
      code: 'BK0475',
      asOf: '2026-07-27',
      requestId: 'sector-eod-resource-test',
    });

    expect(result).toMatchObject({ status: 200, dataVersion });
    if (result.status === 200) {
      expect(result.body).not.toHaveProperty('sectorId');
      expect(result.body).toMatchObject({
        dataVersion,
        inputDataVersions: [
          '00000000-0000-4000-8000-000000000033',
          '00000000-0000-4000-8000-000000000034',
        ],
      });
    }
  });

  /** 验证成分页面固定 release、透传 asOf，并在浏览器边界删除所有内部 UUID。 */
  it('projects observed constituents without internal identifiers', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          sector: {
            sectorId: '00000000-0000-4000-8000-000000000011',
            scheme: 'eastmoney.industry',
            code: 'BK0475',
            name: '证券',
          },
          release: {
            requestedAsOf: '2026-07-27T10:00:00Z',
            resolvedAsOf: '2026-07-27T10:00:00Z',
            coverageStart: '2026-07-20T10:00:00Z',
            membershipSemantics: 'observed',
            qualityStatus: 'passed',
            identityCoveragePercent: '100',
            excludedIdentityCount: 0,
            carriedForwardSectorCount: 0,
            dataVersion: '00000000-0000-4000-8000-000000000012',
            publishedAt: '2026-07-27T11:00:00Z',
          },
          snapshotObservedAt: '2026-07-27T10:00:00Z',
          carriedForward: false,
          items: [
            {
              instrumentId: '00000000-0000-4000-8000-000000000013',
              exchange: 'SSE',
              symbol: '600000',
              name: '浦发银行',
              listingStatus: 'LISTED',
              observedFrom: '2026-07-20T10:00:00Z',
              observedTo: null,
            },
          ],
          nextCursor: null,
        }),
        {
          status: 200,
          headers: {
            ETag: '"membership-v1"',
            'X-Data-Version': '00000000-0000-4000-8000-000000000012',
            'X-Request-Id': 'sector-constituents-test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    const result = await client.listConstituents({
      scheme: 'eastmoney.industry',
      code: 'BK0475',
      asOf: '2026-07-27T10:00:00Z',
      limit: 200,
      requestId: 'sector-constituents-test',
    });

    expect(result).toMatchObject({ status: 200, etag: '"membership-v1"' });
    if (result.status === 200) {
      expect(result.body.sector).not.toHaveProperty('sectorId');
      expect(result.body.items[0]).not.toHaveProperty('instrumentId');
      expect(result.body.items[0]).toMatchObject({
        symbol: '600000',
        observedFrom: '2026-07-20T10:00:00Z',
      });
    }
    const constituentUrl = fetcher.mock.calls[0]?.[0];
    expect(constituentUrl).toBeInstanceOf(URL);
    if (!(constituentUrl instanceof URL)) throw new Error('expected URL request');
    expect(constituentUrl.searchParams.get('asOf')).toBe('2026-07-27T10:00:00Z');
  });

  /** 验证证券反向读取要求 scheme，且同样不把同步服务 UUID 透传给公开调用者。 */
  it('projects reverse membership without internal identifiers', async () => {
    const requestedDataVersion = '00000000-0000-4000-8000-000000000022';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          equity: {
            instrumentId: '00000000-0000-4000-8000-000000000021',
            exchange: 'SSE',
            symbol: '600000',
            name: '浦发银行',
            listingStatus: 'LISTED',
          },
          scheme: 'eastmoney.industry',
          identityAsOf: '2019-12-31',
          dataVersion: requestedDataVersion,
          release: {
            requestedAsOf: null,
            resolvedAsOf: '2026-07-27T10:00:00Z',
            coverageStart: '2026-07-20T10:00:00Z',
            membershipSemantics: 'observed',
            qualityStatus: 'warned',
            identityCoveragePercent: '100',
            excludedIdentityCount: 0,
            carriedForwardSectorCount: 1,
            dataVersion: requestedDataVersion,
            publishedAt: '2026-07-27T11:00:00Z',
          },
          items: [
            {
              sectorId: '00000000-0000-4000-8000-000000000023',
              scheme: 'eastmoney.industry',
              code: 'BK0475',
              name: '证券',
              observedFrom: '2026-07-20T10:00:00Z',
              observedTo: null,
              snapshotObservedAt: '2026-07-27T10:00:00Z',
              carriedForward: true,
            },
          ],
          nextCursor: null,
        }),
        {
          status: 200,
          headers: {
            ETag: '"reverse-membership-v1"',
            'X-Data-Version': requestedDataVersion,
            'X-Request-Id': 'sector-reverse-test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    const result = await client.listEquitySectors({
      exchange: 'SSE',
      symbol: '600000',
      scheme: 'eastmoney.industry',
      dataVersion: requestedDataVersion,
      identityAsOf: '2019-12-31',
      knownAt: '2026-07-30T00:00:00+08:00',
      limit: 200,
      requestId: 'sector-reverse-test',
    });

    if (result.status === 200) {
      expect(result.body.equity).not.toHaveProperty('instrumentId');
      expect(result.body.items[0]).not.toHaveProperty('sectorId');
      expect(result.body.identityAsOf).toBe('2019-12-31');
      expect(result.body.dataVersion).toBe(requestedDataVersion);
      expect(result.body.release.qualityStatus).toBe('warned');
    }
    const equityUrl = fetcher.mock.calls[0]?.[0];
    expect(equityUrl).toBeInstanceOf(URL);
    if (!(equityUrl instanceof URL)) throw new Error('expected URL request');
    expect(equityUrl.searchParams.get('scheme')).toBe('eastmoney.industry');
    expect(equityUrl.searchParams.get('dataVersion')).toBe(requestedDataVersion);
    expect(equityUrl.searchParams.get('identityAsOf')).toBe('2019-12-31');
    expect(equityUrl.searchParams.get('knownAt')).toBe('2026-07-30T00:00:00+08:00');
    expect(equityUrl.searchParams.has('asOf')).toBe(false);
  });

  /** 指定 publication 时，响应头和正文即使彼此一致也不能替换成另一版本。 */
  it('fails closed when reverse membership returns a different publication', async () => {
    const requestedDataVersion = '00000000-0000-4000-8000-000000000024';
    const returnedDataVersion = '00000000-0000-4000-8000-000000000025';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          equity: {
            instrumentId: '00000000-0000-4000-8000-000000000021',
            exchange: 'SSE',
            symbol: '600000',
            name: '代码复用后的证券',
            listingStatus: 'LISTED',
          },
          scheme: 'eastmoney.industry',
          identityAsOf: '2026-07-29',
          dataVersion: returnedDataVersion,
          release: {
            requestedAsOf: null,
            resolvedAsOf: '2026-07-27T10:00:00Z',
            coverageStart: '2026-07-20T10:00:00Z',
            membershipSemantics: 'observed',
            qualityStatus: 'passed',
            identityCoveragePercent: '100',
            excludedIdentityCount: 0,
            carriedForwardSectorCount: 0,
            dataVersion: returnedDataVersion,
            publishedAt: '2026-07-27T11:00:00Z',
          },
          items: [],
          nextCursor: null,
        }),
        {
          status: 200,
          headers: {
            ETag: '"reverse-membership-v2"',
            'X-Data-Version': returnedDataVersion,
            'X-Request-Id': 'sector-version-mismatch-test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    await expect(
      client.listEquitySectors({
        exchange: 'SSE',
        symbol: '600000',
        scheme: 'eastmoney.industry',
        dataVersion: requestedDataVersion,
        identityAsOf: '2026-07-29',
        limit: 200,
        requestId: 'sector-version-mismatch-test',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });

  /** 下游若回显另一业务身份日，即使 publication 正确也必须按契约漂移失败关闭。 */
  it('fails closed when reverse membership returns a different identity date', async () => {
    const dataVersion = '00000000-0000-4000-8000-000000000026';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          equity: {
            instrumentId: '00000000-0000-4000-8000-000000000021',
            exchange: 'SSE',
            symbol: '600000',
            name: '代码复用后的证券',
            listingStatus: 'LISTED',
          },
          scheme: 'eastmoney.industry',
          identityAsOf: '2026-07-30',
          dataVersion,
          release: {
            requestedAsOf: null,
            resolvedAsOf: '2026-07-27T10:00:00Z',
            coverageStart: '2026-07-20T10:00:00Z',
            membershipSemantics: 'observed',
            qualityStatus: 'passed',
            identityCoveragePercent: '100',
            excludedIdentityCount: 0,
            carriedForwardSectorCount: 0,
            dataVersion,
            publishedAt: '2026-07-27T11:00:00Z',
          },
          items: [],
          nextCursor: null,
        }),
        {
          status: 200,
          headers: {
            ETag: '"reverse-membership-v3"',
            'X-Data-Version': dataVersion,
            'X-Request-Id': 'sector-identity-mismatch-test',
          },
        },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    await expect(
      client.listEquitySectors({
        exchange: 'SSE',
        symbol: '600000',
        scheme: 'eastmoney.industry',
        dataVersion,
        identityAsOf: '2026-07-29',
        limit: 200,
        requestId: 'sector-identity-mismatch-test',
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });
});
