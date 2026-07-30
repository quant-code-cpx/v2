import { describe, expect, it, vi } from 'vitest';

import type { RedisService } from '../../../shared/redis/redis.service.js';
import { EquityWorkspaceRateLimitService } from '../equity-workspace-rate-limit.service.js';

/** 覆盖股票中心每用户短期限流及 Redis 故障时的 fail-closed 行为。 */
describe('EquityWorkspaceRateLimitService', () => {
  /** 复杂搜索应同时消费基础桶和独立成本桶。 */
  it('counts normal and complex search buckets', async () => {
    const redis = { incrementWithTtl: vi.fn().mockResolvedValue(1) };
    const service = new EquityWorkspaceRateLimitService(redis as unknown as RedisService);

    await service.assertSearchAllowed('user-1', {
      memberships: [{ scheme: 'EASTMONEY_INDUSTRY', code: 'BK0475' }],
      limit: 50,
    });

    expect(redis.incrementWithTtl).toHaveBeenCalledTimes(2);
    expect(redis.incrementWithTtl).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining('equity-workspace:search:'),
      60,
    );
    expect(redis.incrementWithTtl).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('equity-workspace:search-complex:'),
      60,
    );
  });

  /** 超过固定窗口上限应返回带 Retry-After 的稳定 429。 */
  it('rejects requests above the user limit', async () => {
    const redis = { incrementWithTtl: vi.fn().mockResolvedValue(61) };
    const service = new EquityWorkspaceRateLimitService(redis as unknown as RedisService);

    await expect(service.assertSearchAllowed('user-1', { limit: 50 })).rejects.toMatchObject({
      status: 429,
      response: { code: 'rate-limited', retryAfter: 60 },
    });
  });

  /** Redis 不可用时不得绕过防滥用边界或回退到进程内不一致计数。 */
  it('fails closed when Redis is unavailable', async () => {
    const redis = { incrementWithTtl: vi.fn().mockRejectedValue(new Error('unavailable')) };
    const service = new EquityWorkspaceRateLimitService(redis as unknown as RedisService);

    await expect(service.assertDataStatusAllowed('user-1')).rejects.toMatchObject({
      status: 503,
      response: { code: 'dependency-unavailable' },
    });
  });
});
