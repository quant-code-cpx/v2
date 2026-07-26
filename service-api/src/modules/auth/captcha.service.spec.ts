/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest asymmetric matchers intentionally return `any`. */

import type { AppConfigService } from '../../platform/config/app-config.service.js';
import type { RedisService } from '../../platform/redis/redis.service.js';
import { describe, expect, it } from 'vitest';

import {
  CaptchaService,
  type CaptchaCodeGenerator,
  type CaptchaClientContext,
} from './captcha.service.js';
import type { SecurityRateLimitService } from './security-rate-limit.service.js';

const configuration = {
  captchaHmacSecret: 'test-only-captcha-hmac-secret-long-enough-for-hmac-use',
  captchaTtlSeconds: 120,
} as AppConfigService;

const clientA: CaptchaClientContext = { ip: '127.0.0.1', userAgent: 'fixture-browser' };
const clientB: CaptchaClientContext = { ip: '127.0.0.2', userAgent: 'fixture-browser' };

// Group CAPTCHA one-time, cross-context, and dependency-fail-closed tests around a test-only code fixture.
describe('CaptchaService', () => {
  // Verify public challenge response contains only PNG challenge data, never test answer state.
  it('issues PNG challenge without exposing answer and consumes correct answer once', async () => {
    const redis = new FakeRedis();
    const service = serviceWith(redis);

    const challenge = await service.createChallenge(clientA);

    expect(challenge).toMatchObject({
      imageDataUrl: expect.stringMatching(/^data:image\/png;base64,/),
    });
    expect(challenge).not.toHaveProperty('answer');
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      true,
    );
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      false,
    );
  });

  // Verify an incorrect answer is terminal and cannot be retried against same challenge ID.
  it('consumes an incorrect answer before returning failure', async () => {
    const service = serviceWith(new FakeRedis());
    const challenge = await service.createChallenge(clientA);

    await expect(service.verifyAndConsume(challenge.challengeId, '1111', clientA)).resolves.toBe(
      false,
    );
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      false,
    );
  });

  // Verify IP-bound challenge cannot be solved from another client context.
  it('rejects cross-IP CAPTCHA use and consumes the challenge', async () => {
    const service = serviceWith(new FakeRedis());
    const challenge = await service.createChallenge(clientA);

    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientB)).resolves.toBe(
      false,
    );
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      false,
    );
  });

  // Verify security-state dependency loss blocks issuance rather than creating a bypassable challenge.
  it('fails closed with 503 when Redis cannot persist CAPTCHA state', async () => {
    const service = serviceWith(new ThrowingRedis());

    await expect(service.createChallenge(clientA)).rejects.toMatchObject({ status: 503 });
  });
});

/** Hold short-lived test challenge digests with same one-time comparison semantics as Redis Lua script. */
class FakeRedis {
  private readonly values = new Map<string, string>();

  /** Persist fixture digest under opaque challenge key. */
  public set(key: string, value: string): Promise<void> {
    this.values.set(key, value);
    return Promise.resolve();
  }

  /** Atomically remove fixture digest before comparing candidate value. */
  public consumeMatchingValue(key: string, expectedValue: string): Promise<boolean> {
    const value = this.values.get(key);
    this.values.delete(key);
    return Promise.resolve(value === expectedValue);
  }
}

/** Simulate unavailable Redis only for fail-closed CAPTCHA regression coverage. */
class ThrowingRedis {
  /** Reject write so service must return dependency-unavailable instead of a usable challenge. */
  public set(): Promise<void> {
    return Promise.reject(new Error('Redis unavailable'));
  }

  /** Keep interface shape available if future test flow reaches verification. */
  public consumeMatchingValue(): Promise<boolean> {
    return Promise.reject(new Error('Redis unavailable'));
  }
}

/** Return a fixture-safe static test code only through direct unit-test injection, never production config. */
class FixtureCaptchaCodeGenerator implements CaptchaCodeGenerator {
  /** Generate known test answer used by this test file without becoming an HTTP/API response field. */
  public generate(): string {
    return '2468';
  }
}

/** Construct CAPTCHA service with test-only generator and an issuance limiter that always permits local fixtures. */
function serviceWith(redis: FakeRedis | ThrowingRedis): CaptchaService {
  const rateLimit = {
    assertCaptchaIssueAllowed: (): Promise<void> => Promise.resolve(),
  } as unknown as SecurityRateLimitService;
  return new CaptchaService(
    redis as unknown as RedisService,
    rateLimit,
    configuration,
    new FixtureCaptchaCodeGenerator(),
  );
}
