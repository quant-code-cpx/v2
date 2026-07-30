import type { NextFunction, Request, Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { requestIdMiddleware } from '../middleware/request-id.middleware.js';

/** 覆盖公开请求标识只生成一次、原样传播和非法值拒绝。 */
describe('requestIdMiddleware', () => {
  /** 验证合法客户端请求标识原样写入请求与响应。 */
  it('preserves a valid client request ID', () => {
    const request = requestWithId('client/market:request-1');
    const response = responseStub();
    const next = vi.fn();

    requestIdMiddleware(request, response.value, next as NextFunction);

    expect(request.requestId).toBe('client/market:request-1');
    expect(response.setHeader).toHaveBeenCalledWith('x-request-id', 'client/market:request-1');
    expect(next).toHaveBeenCalledOnce();
  });

  /** 验证请求未提供标识时只生成一个 UUID 并复用。 */
  it('generates one request ID only when absent', () => {
    const request = requestWithId(undefined);
    const response = responseStub();
    const next = vi.fn();

    requestIdMiddleware(request, response.value, next as NextFunction);

    expect(request.requestId).toMatch(/^[0-9a-f-]{36}$/);
    expect(response.setHeader).toHaveBeenCalledWith('x-request-id', request.requestId);
  });

  /** 验证过长或非法字符请求标识确定性返回公开校验错误。 */
  it('rejects invalid request IDs instead of silently replacing them', () => {
    const request = requestWithId('invalid request id');
    const response = responseStub();

    /** 触发非法请求标识校验。 */
    function invokeInvalidRequestId(): void {
      requestIdMiddleware(request, response.value, vi.fn() as NextFunction);
    }

    expect(invokeInvalidRequestId).toThrowError('X-Request-Id is invalid');
    expect(request.requestId).toMatch(/^[0-9a-f-]{36}$/);
    expect(response.setHeader).toHaveBeenCalledWith('x-request-id', request.requestId);
  });
});

/** 构造只暴露 X-Request-Id 读取的 Express 请求。 */
function requestWithId(requestId: string | undefined): Request {
  return { header: vi.fn().mockReturnValue(requestId) } as unknown as Request;
}

/** 构造可观察响应头写入的 Express 响应。 */
function responseStub(): {
  value: Response;
  setHeader: ReturnType<typeof vi.fn>;
} {
  const setHeader = vi.fn();
  return { value: { setHeader } as unknown as Response, setHeader };
}
