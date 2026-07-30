import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { RedisService } from '../../../shared/redis/redis.service.js';
import { StockConnectRateLimitService } from '../stock-connect-rate-limit.service.js';

/** 覆盖互联互通用户级短期安全限流和 Redis 失败关闭。 */
describe('StockConnectRateLimitService', () => {
  /** 验证总览每分钟六十次后返回稳定 429 与 Retry-After。 */
  it('rejects overview traffic above its fixed window limit', async () => {
    const redis = { incrementWithTtl: vi.fn().mockResolvedValue(61) };
    const service = new StockConnectRateLimitService(redis as unknown as RedisService);

    await expect(service.assertAllowed('user-1', 'OVERVIEW')).rejects.toMatchObject({
      status: HttpStatus.TOO_MANY_REQUESTS,
      response: { code: 'RATE_LIMITED', retryAfter: 60 },
    });
    expect(redis.incrementWithTtl).toHaveBeenCalledWith(
      expect.stringMatching(/^stock-connect:OVERVIEW:/),
      60,
    );
  });

  /** 验证 Redis 不可用时不绕过安全限流。 */
  it('fails closed when Redis is unavailable', async () => {
    const redis = {
      incrementWithTtl: vi.fn().mockRejectedValue(new Error('redis unavailable')),
    };
    const service = new StockConnectRateLimitService(redis as unknown as RedisService);

    await expect(service.assertAllowed('user-1', 'SECURITY_CONTEXT')).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      response: { code: 'UPSTREAM_UNAVAILABLE' },
    });
  });
});
