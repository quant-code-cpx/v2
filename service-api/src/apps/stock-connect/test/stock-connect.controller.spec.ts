import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { StockConnectController } from '../stock-connect.controller.js';
import type { StockConnectRateLimitService } from '../stock-connect-rate-limit.service.js';
import type { StockConnectService } from '../stock-connect.service.js';

/** 覆盖五条公开 POST 的主体限流、请求标识传播和 204 完整响应头。 */
describe('StockConnectController', () => {
  /** 验证五个读取 operation 使用认证用户独立限流桶并原样传递 requestId。 */
  it('delegates all authenticated reads with the same request ID', async () => {
    const stockConnect = {
      overview: vi.fn().mockResolvedValue(successResult('overview')),
      readiness: vi.fn().mockResolvedValue(successResult('readiness')),
      channel: vi.fn().mockResolvedValue(successResult('channel')),
      activeSecurities: vi.fn().mockResolvedValue(successResult('active')),
      securityContext: vi.fn().mockResolvedValue(successResult('security')),
    };
    const rateLimit = { assertAllowed: vi.fn().mockResolvedValue(undefined) };
    const controller = new StockConnectController(
      stockConnect as unknown as StockConnectService,
      rateLimit as unknown as StockConnectRateLimitService,
    );
    const request = { requestId: 'req-controller', user: { userId: 'user-1' } } as never;

    await controller.overview({} as never, undefined, request, response().value);
    await controller.readiness({} as never, undefined, request, response().value);
    await controller.channel({} as never, undefined, request, response().value);
    await controller.activeSecurities({} as never, undefined, request, response().value);
    await controller.securityContext({} as never, undefined, request, response().value);

    expect(rateLimit.assertAllowed).toHaveBeenNthCalledWith(1, 'user-1', 'OVERVIEW');
    expect(rateLimit.assertAllowed).toHaveBeenNthCalledWith(2, 'user-1', 'READINESS');
    expect(rateLimit.assertAllowed).toHaveBeenNthCalledWith(3, 'user-1', 'CHANNEL');
    expect(rateLimit.assertAllowed).toHaveBeenNthCalledWith(4, 'user-1', 'ACTIVE_SECURITIES');
    expect(rateLimit.assertAllowed).toHaveBeenNthCalledWith(5, 'user-1', 'SECURITY_CONTEXT');
    expect(stockConnect.overview).toHaveBeenCalledWith({}, undefined, 'req-controller');
    expect(stockConnect.readiness).toHaveBeenCalledWith({}, undefined, 'req-controller');
    expect(stockConnect.channel).toHaveBeenCalledWith({}, undefined, 'req-controller');
    expect(stockConnect.activeSecurities).toHaveBeenCalledWith({}, undefined, 'req-controller');
    expect(stockConnect.securityContext).toHaveBeenCalledWith({}, undefined, 'req-controller');
  });

  /** 验证条件命中响应仍携带 Cache-Control、X-Data-Version 与强 ETag。 */
  it('writes complete cache headers on a 204 response', async () => {
    const stockConnect = {
      overview: vi.fn().mockResolvedValue({
        status: 204,
        dataVersion: 'bundle-v1',
        etag: '"representation-etag"',
      }),
    };
    const rateLimit = { assertAllowed: vi.fn().mockResolvedValue(undefined) };
    const controller = new StockConnectController(
      stockConnect as unknown as StockConnectService,
      rateLimit as unknown as StockConnectRateLimitService,
    );
    const output = response();

    const result = await controller.overview(
      {} as never,
      '"representation-etag"',
      { requestId: 'req-204', user: { userId: 'user-1' } } as never,
      output.value,
    );

    expect(result).toBeUndefined();
    expect(output.setHeader).toHaveBeenCalledWith('Cache-Control', 'private, no-cache');
    expect(output.setHeader).toHaveBeenCalledWith('X-Data-Version', 'bundle-v1');
    expect(output.setHeader).toHaveBeenCalledWith('ETag', '"representation-etag"');
    expect(output.status).toHaveBeenCalledWith(204);
    expect(output.send).toHaveBeenCalledOnce();
  });
});

/** 构造控制器正常返回所需的条件读取结果。 */
function successResult(resource: string): {
  status: 200;
  dataVersion: string;
  etag: string;
  body: { resource: string };
} {
  return {
    status: 200,
    dataVersion: 'bundle-v1',
    etag: '"representation-etag"',
    body: { resource },
  };
}

/** 构造控制器响应头与 204 发送行为的可观察替身。 */
function response(): {
  value: Response;
  setHeader: ReturnType<typeof vi.fn>;
  status: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
} {
  const send = vi.fn();
  const status = vi.fn().mockReturnValue({ send });
  const setHeader = vi.fn();
  return {
    value: { setHeader, status } as never,
    setHeader,
    status,
    send,
  };
}
