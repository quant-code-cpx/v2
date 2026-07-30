import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import { PublicProblemException } from '../../../common/exceptions/problem.exception.js';
import { MarketDataAccessController } from '../market-data-access.controller.js';
import type { MarketDataAccessService } from '../market-data-access.service.js';

/** 覆盖通用市场数据公开 POST 路由的安全委派。 */
describe('MarketDataAccessController', () => {
  /** 请求标识和未经修改的查询体必须传给应用服务，空页不在 Controller 层改写。 */
  it('delegates a query and preserves the empty response', async () => {
    const expected = {
      meta: { availability: 'SOURCE_UNAVAILABLE' },
      records: [],
    };
    const marketData = { query: vi.fn().mockResolvedValue(expected) };
    const controller = new MarketDataAccessController(
      marketData as unknown as MarketDataAccessService,
    );
    const body = { dataset: { code: 'derivative.bar.1d.reported', schemaVersion: 1 } };

    const result = await controller.query(body, { requestId: 'req-market-data' } as never);

    expect(marketData.query).toHaveBeenCalledWith(body, 'req-market-data');
    expect(result).toBe(expected);
  });

  /** 下游合同漂移映射出的安全 503 必须原样越过 Controller，不能泄漏内部字段或被改写。 */
  it('preserves a safe dependency-unavailable problem', async () => {
    const problem = new PublicProblemException(
      HttpStatus.SERVICE_UNAVAILABLE,
      'dependency-unavailable',
      'Market data is temporarily unavailable',
    );
    const marketData = { query: vi.fn().mockRejectedValue(problem) };
    const controller = new MarketDataAccessController(
      marketData as unknown as MarketDataAccessService,
    );

    await expect(
      controller.query({}, { requestId: 'req-market-data-unavailable' } as never),
    ).rejects.toBe(problem);
  });

  /** 公开 typed publication 响应必须声明私有且不落共享缓存。 */
  it('marks the public query response as private and no-store', () => {
    const controllerPrototype = MarketDataAccessController.prototype as unknown as Record<
      string,
      unknown
    >;
    const queryHandler = controllerPrototype['query'];
    if (typeof queryHandler !== 'function') throw new TypeError('Expected query handler');
    const headers: unknown = Reflect.getMetadata('__headers__', queryHandler) as unknown;

    expect(headers).toContainEqual({ name: 'Cache-Control', value: 'private, no-store' });
  });
});
