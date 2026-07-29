import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { MarketDataAccessClient } from '../clients/market-data-access.client.js';

/** 提供通用市场数据内部 POST 调用所需的最小配置。 */
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

/** 覆盖成功空页的内部 POST 方法、服务身份和严格合同投影。 */
describe('MarketDataAccessClient', () => {
  /** 未发布数据集必须被接受为可显示的空 records，而非被错误映射为 503。 */
  it('forwards a query and accepts a successful source-unavailable empty page', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          meta: {
            requestId: '00000000-0000-4000-8000-000000000101',
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
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const client = new MarketDataAccessClient(config, fetcher);

    const result = await client.query({ request: requestBody(), requestId: 'req-market-data' });

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
        'X-Request-Id': 'req-market-data',
      },
    });
    if (typeof init?.body !== 'string')
      throw new Error('market-data request body must be JSON text');
    expect(JSON.parse(init.body)).toMatchObject({
      dataset: { code: 'derivative.bar.1d.reported', schemaVersion: 1 },
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
