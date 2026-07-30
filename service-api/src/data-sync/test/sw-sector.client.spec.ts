import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import { SwSectorClient } from '../clients/sw-sector.client.js';

const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
} as AppConfigService;

/** 覆盖申万下游认证、查询透传、Zod 合同和错误隔离。 */
describe('SwSectorClient', () => {
  /** 验证 taxonomy 页严格校验方法学并携带内部凭据与条件头。 */
  it('validates taxonomy release and forwards bounded query parameters', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(page()), {
        status: 200,
        headers: {
          ETag: '"sw-v1"',
          'Content-Type': 'application/json',
          'X-Data-Version': '00000000-0000-4000-8000-000000000001',
          'X-Request-Id': 'sw/client:test-request',
        },
      }),
    );
    const client = new SwSectorClient(config, fetcher);

    const result = await client.listIndustries({
      snapshotDate: '2026-07-28',
      level: 2,
      parentCode: '801010.SI',
      limit: 100,
      ifNoneMatch: '"sw-v0"',
      requestId: 'sw/client:test-request',
    });

    expect(result).toMatchObject({ status: 200, etag: '"sw-v1"' });
    const [request, init] = fetcher.mock.calls[0] ?? [];
    expect(request).toBeInstanceOf(URL);
    if (!(request instanceof URL)) throw new Error('expected URL request');
    expect(request.pathname).toBe('/internal/v1/sw-industries');
    expect(request.searchParams.get('parentCode')).toBe('801010.SI');
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalBearerToken}`,
      'If-None-Match': '"sw-v0"',
      'X-Request-Id': 'sw/client:test-request',
    });
  });

  /** 验证下游 schema 漂移不会向公开调用者泄漏，而是稳定映射为 503。 */
  it('maps invalid downstream methodology to dependency unavailable', async () => {
    const invalid = page();
    invalid.release.methodology.semanticSpecSha256 = 'invalid';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(invalid), {
        status: 200,
        headers: {
          ETag: '"sw-invalid-v1"',
          'Content-Type': 'application/json',
          'X-Data-Version': invalid.release.dataVersion,
          'X-Request-Id': 'sw-invalid-contract-test',
        },
      }),
    );
    const client = new SwSectorClient(config, fetcher);

    await expect(
      client.listIndustries({ limit: 100, requestId: 'sw-invalid-contract-test' }),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'dependency-unavailable' },
    });
  });
});

/** 构造符合 0020 合同的最小 taxonomy 分页。 */
function page(): {
  scheme: 'sw.industry';
  release: {
    snapshotDate: string;
    dataVersion: string;
    publishedAt: string;
    qualityStatus: 'passed';
    rowCount: number;
    methodology: {
      code: string;
      version: number;
      status: 'source_reported';
      upstreamSource: string;
      semanticSpecSha256: string;
    };
  };
  items: Array<{
    code: string;
    name: string;
    level: number;
    parentCode: string;
    componentCount: number;
    revision: number;
  }>;
  nextCursor: null;
} {
  return {
    scheme: 'sw.industry',
    release: {
      snapshotDate: '2026-07-28',
      dataVersion: '00000000-0000-4000-8000-000000000001',
      publishedAt: '2026-07-28T10:00:00Z',
      qualityStatus: 'passed',
      rowCount: 3,
      methodology: {
        code: 'legulegu-sw-industry-overview',
        version: 1,
        status: 'source_reported',
        upstreamSource: 'legulegu.sw-industry-overview',
        semanticSpecSha256: 'a'.repeat(64),
      },
    },
    items: [
      {
        code: '801016.SI',
        name: '种植业',
        level: 2,
        parentCode: '801010.SI',
        componentCount: 20,
        revision: 1,
      },
    ],
    nextCursor: null,
  };
}
