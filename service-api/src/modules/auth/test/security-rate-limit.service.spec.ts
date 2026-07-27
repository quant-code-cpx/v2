import { describe, expect, it } from 'vitest';

import type { AppConfigService } from '../../../platform/config/app-config.service.js';
import type { RedisService } from '../../../platform/redis/redis.service.js';
import { SecurityRateLimitService } from '../security-rate-limit.service.js';

class FakeRedis {
  private readonly values = new Map<string, string>();
  private readonly counters = new Map<string, number>();

  /** 返回限流键断言所需的当前 fixture 值。 */
  public get(key: string): Promise<string | null> {
    return Promise.resolve(this.values.get(key) ?? null);
  }

  /** 保存限流键断言所需的 fixture 值。 */
  public set(key: string, value: string): Promise<void> {
    this.values.set(key, value);
    return Promise.resolve();
  }

  /** 删除指定键的 fixture 计数器与锁定状态。 */
  public delete(key: string): Promise<void> {
    this.values.delete(key);
    this.counters.delete(key);
    return Promise.resolve();
  }

  /** 为单元测试模拟单调递增的固定窗口计数器。 */
  public incrementWithTtl(key: string): Promise<number> {
    const value = (this.counters.get(key) ?? 0) + 1;
    this.counters.set(key, value);
    return Promise.resolve(value);
  }
}

const configuration = {
  loginFailureWindowSeconds: 60,
  loginLockSeconds: 60,
  loginMaxFailures: 2,
  captchaRateLimitWindowSeconds: 60,
  captchaRateLimitMax: 2,
  refreshRateLimitWindowSeconds: 60,
  refreshRateLimitMax: 2,
  refreshTokenTtlSeconds: 3_600,
} as AppConfigService;

// 汇集可观察锁定、限流与重放结果的安全控制回归测试。
describe('SecurityRateLimitService', () => {
  // 验证失败阈值会触发临时登录锁定。
  it('locks login after configured failures', async () => {
    const service = new SecurityRateLimitService(
      new FakeRedis() as unknown as RedisService,
      configuration,
    );

    await service.recordFailedLogin('market.user', '127.0.0.1');
    await service.recordFailedLogin('market.user', '127.0.0.1');

    await expect(service.assertLoginAllowed('market.user', '127.0.0.1')).rejects.toMatchObject({
      status: 429,
    });
  });

  // 验证即使尚无登录尝试，验证码签发流量也独立受限。
  it('limits CAPTCHA issue requests by client address', async () => {
    const service = new SecurityRateLimitService(
      new FakeRedis() as unknown as RedisService,
      configuration,
    );

    await service.assertCaptchaIssueAllowed('127.0.0.1');
    await service.assertCaptchaIssueAllowed('127.0.0.1');

    await expect(service.assertCaptchaIssueAllowed('127.0.0.1')).rejects.toMatchObject({
      status: 429,
    });
  });

  // 验证 refresh 计数器按会话与客户端地址隔离重复尝试。
  it('limits refresh requests per session and address', async () => {
    const service = new SecurityRateLimitService(
      new FakeRedis() as unknown as RedisService,
      configuration,
    );

    await service.assertRefreshAllowed('session-id', '127.0.0.1');
    await service.assertRefreshAllowed('session-id', '127.0.0.1');

    await expect(service.assertRefreshAllowed('session-id', '127.0.0.1')).rejects.toMatchObject({
      status: 429,
    });
  });

  // 验证检测到 refresh 重用后会留下可查询的失败关闭标记。
  it('retains a short-lived replay marker for a detected refresh-token replay', async () => {
    const service = new SecurityRateLimitService(
      new FakeRedis() as unknown as RedisService,
      configuration,
    );

    await service.markRefreshReplay('session-id');

    await expect(service.isRefreshReplayMarked('session-id')).resolves.toBe(true);
  });
});
