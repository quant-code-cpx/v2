import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../platform/config/app-config.service.js';
import { SectorMarketDataClient } from './sector-market-data.client.js';

const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

/** 覆盖下游合同投影、认证边界与安全错误映射。 */
describe('SectorMarketDataClient', () => {
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
        { status: 200, headers: { ETag: '"catalog-v1"', 'Content-Type': 'application/json' } },
      ),
    );
    const client = new SectorMarketDataClient(config, fetcher);

    const result = await client.listSectors({
      scheme: 'eastmoney.industry',
      limit: 100,
      ifNoneMatch: '"catalog-v0"',
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
    });
  });

  /** 验证下游快照冲突不会泄漏响应体，而是变成公开稳定问题码。 */
  it('maps upstream snapshot conflicts to the public problem contract', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 409 }));
    const client = new SectorMarketDataClient(config, fetcher);

    await expect(
      client.listBars({
        scheme: 'eastmoney.industry',
        code: 'BK0475',
        period: '1w',
        start: '2026-06-01',
        end: '2026-06-30',
        limit: 100,
      }),
    ).rejects.toMatchObject({
      status: HttpStatus.CONFLICT,
      response: { code: 'snapshot-expired' },
    });
  });
});
