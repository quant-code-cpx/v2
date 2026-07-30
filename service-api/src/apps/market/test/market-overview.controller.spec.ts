import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { Response } from 'express';

import type { AuthenticatedRequest } from '../../../common/models/auth-context.js';
import { createMarketOverviewFixture } from '../../../data-sync/test/market-overview.fixtures.js';
import {
  ListMarketIndexBarsBodyDto,
  MarketIndexPathDto,
  MarketOverviewBodyDto,
} from '../dto/market-overview.dto.js';
import {
  MarketOverviewController,
  writeMarketConditionalResponse,
} from '../market-overview.controller.js';
import type { MarketOverviewService } from '../market-overview.service.js';

/** 保存 Express 响应与可直接断言的 mock，避免解绑定真实方法。 */
type ResponseDouble = {
  response: Response;
  setHeader: ReturnType<typeof vi.fn>;
  status: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
};

/** 构造可观察缓存头、状态与 send 调用的 Express 响应替身。 */
function responseDouble(): ResponseDouble {
  const response = {
    setHeader: vi.fn(),
    status: vi.fn(),
    send: vi.fn(),
  };
  response.status.mockReturnValue(response);
  return {
    response: response as unknown as Response,
    setHeader: response.setHeader,
    status: response.status,
    send: response.send,
  };
}

describe('writeMarketConditionalResponse', () => {
  /** 验证 200 响应复制强 ETag、dataVersion 与私有重验证缓存策略。 */
  it('writes publication cache headers for a successful response', () => {
    const response = responseDouble();
    const body = createMarketOverviewFixture();

    const output = writeMarketConditionalResponse(response.response, {
      status: 200,
      etag: '"market-v1"',
      dataVersion: body.dataVersion,
      body,
    });

    expect(output).toBe(body);
    expect(response.setHeader).toHaveBeenCalledWith('ETag', '"market-v1"');
    expect(response.setHeader).toHaveBeenCalledWith('X-Data-Version', body.dataVersion);
    expect(response.setHeader).toHaveBeenCalledWith(
      'Cache-Control',
      'private, max-age=0, must-revalidate',
    );
  });

  /** 验证内部 GET 304 在公开 POST 边界映射为无响应体的 204。 */
  it('maps internal not-modified to public POST 204', () => {
    const response = responseDouble();

    const output = writeMarketConditionalResponse(response.response, {
      status: 304,
      etag: '"market-v1"',
      dataVersion: '00000000-0000-4000-8000-000000000001',
    });

    expect(output).toBeUndefined();
    expect(response.status).toHaveBeenCalledWith(HttpStatus.NO_CONTENT);
    expect(response.send).toHaveBeenCalledOnce();
  });
});

describe('MarketOverviewController', () => {
  /** 验证公开首页只调用单 bundle 应用服务并传递关联标识。 */
  it('delegates overview to one atomic bundle read', async () => {
    const body = createMarketOverviewFixture();
    const getOverview = vi.fn().mockResolvedValue({
      status: 200,
      etag: '"market-v1"',
      dataVersion: body.dataVersion,
      body,
    });
    const service = {
      getOverview,
    } as unknown as MarketOverviewService;
    const controller = new MarketOverviewController(service);
    const request = { requestId: 'request-controller' } as AuthenticatedRequest & {
      requestId: string;
    };
    const response = responseDouble();
    const input = Object.assign(new MarketOverviewBodyDto(), { asOf: '2026-07-30' });

    await expect(
      controller.getOverview(input, '"previous"', request, response.response),
    ).resolves.toEqual(body);
    expect(getOverview).toHaveBeenCalledWith(input, '"previous"', 'request-controller');
  });

  /** 验证四个稳定指数身份都由同一公开路由原样交给应用服务。 */
  it('delegates every primary index identity through the index bars route', async () => {
    const listIndexBars = vi.fn().mockResolvedValue({
      status: 200,
      etag: '"index-v1"',
      dataVersion: '00000000-0000-4000-8000-000000000005',
      body: { dataVersion: '00000000-0000-4000-8000-000000000005' },
    });
    const service = { listIndexBars } as unknown as MarketOverviewService;
    const controller = new MarketOverviewController(service);
    const request = { requestId: 'request-index-route' } as AuthenticatedRequest & {
      requestId: string;
    };
    const input = Object.assign(new ListMarketIndexBarsBodyDto(), {
      period: '1d',
      start: '2026-01-01',
      end: '2026-07-30',
    });

    for (const indexId of ['sse-composite', 'szse-component', 'csi-300', 'chinext'] as const) {
      const path = Object.assign(new MarketIndexPathDto(), { indexId });
      await controller.listIndexBars(path, input, undefined, request, responseDouble().response);
      expect(listIndexBars).toHaveBeenLastCalledWith(path, input, undefined, 'request-index-route');
    }
    expect(listIndexBars).toHaveBeenCalledTimes(4);
  });
});
