import { describe, expect, it } from 'vitest';

import type { AppConfigService } from '../../platform/config/app-config.service.js';
import type { RedisService } from '../../platform/redis/redis.service.js';
import { SecurityRateLimitService } from './security-rate-limit.service.js';

class FakeRedis {
  private readonly values = new Map<string, string>();
  private readonly counters = new Map<string, number>();

  public get(key: string): Promise<string | null> {
    return Promise.resolve(this.values.get(key) ?? null);
  }

  public set(key: string, value: string): Promise<void> {
    this.values.set(key, value);
    return Promise.resolve();
  }

  public delete(key: string): Promise<void> {
    this.values.delete(key);
    this.counters.delete(key);
    return Promise.resolve();
  }

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
  refreshRateLimitWindowSeconds: 60,
  refreshRateLimitMax: 2,
  refreshTokenTtlSeconds: 3_600,
} as AppConfigService;

describe('SecurityRateLimitService', () => {
  it('locks login after configured failures', async () => {
    const service = new SecurityRateLimitService(
      new FakeRedis() as unknown as RedisService,
      configuration,
    );

    await service.recordFailedLogin('user@example.com', '127.0.0.1');
    await service.recordFailedLogin('user@example.com', '127.0.0.1');

    await expect(service.assertLoginAllowed('user@example.com', '127.0.0.1')).rejects.toMatchObject(
      {
        status: 429,
      },
    );
  });

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

  it('retains a short-lived replay marker for a detected refresh-token replay', async () => {
    const service = new SecurityRateLimitService(
      new FakeRedis() as unknown as RedisService,
      configuration,
    );

    await service.markRefreshReplay('session-id');

    await expect(service.isRefreshReplayMarked('session-id')).resolves.toBe(true);
  });
});
