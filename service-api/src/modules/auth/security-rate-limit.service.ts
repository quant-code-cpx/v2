import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';

import { AppConfigService } from '../../platform/config/app-config.service.js';
import { PublicProblemException } from '../../platform/http/problem.exception.js';
import { RedisService } from '../../platform/redis/redis.service.js';

class RateLimitedException extends PublicProblemException {
  /** Create service-specific HTTP 429 response without exposing internal rate-limit state. */
  public constructor(message: string, retryAfter: number) {
    super(HttpStatus.TOO_MANY_REQUESTS, 'rate-limited', message, retryAfter);
  }
}

@Injectable()
export class SecurityRateLimitService {
  /** Coordinate login and refresh safeguards through short-lived Redis keys. */
  public constructor(
    private readonly redis: RedisService,
    private readonly config: AppConfigService,
  ) {}

  /** Reject a login identity currently locked after repeated failed attempts. */
  public async assertLoginAllowed(account: string, ip: string): Promise<void> {
    try {
      for (const identifier of this.loginIdentifiers(account, ip)) {
        if (await this.redis.get(`auth:login:locked:${identifier}`)) {
          throw new RateLimitedException('Too many login attempts', this.config.loginLockSeconds);
        }
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Count failed login, then atomically convert threshold breach into temporary lock. */
  public async recordFailedLogin(account: string, ip: string): Promise<void> {
    try {
      for (const identifier of this.loginIdentifiers(account, ip)) {
        const attempts = await this.redis.incrementWithTtl(
          `auth:login:failed:${identifier}`,
          this.config.loginFailureWindowSeconds,
        );
        // Delete counter once locked so another request cannot extend failure window unexpectedly.
        if (attempts >= this.config.loginMaxFailures) {
          await this.redis.set(
            `auth:login:locked:${identifier}`,
            '1',
            this.config.loginLockSeconds,
          );
          await this.redis.delete(`auth:login:failed:${identifier}`);
        }
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Remove prior login-failure counter after verified successful authentication. */
  public async resetLoginFailures(account: string, ip: string): Promise<void> {
    try {
      for (const identifier of this.loginIdentifiers(account, ip)) {
        await this.redis.delete(`auth:login:failed:${identifier}`);
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Bound CAPTCHA issuance by client network identity before expensive image generation. */
  public async assertCaptchaIssueAllowed(ip: string): Promise<void> {
    try {
      const requests = await this.redis.incrementWithTtl(
        `auth:captcha:issued:${this.identifier(ip)}`,
        this.config.captchaRateLimitWindowSeconds,
      );
      if (requests > this.config.captchaRateLimitMax) {
        throw new RateLimitedException(
          'Too many CAPTCHA requests',
          this.config.captchaRateLimitWindowSeconds,
        );
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Enforce bounded refresh attempts per session and network address. */
  public async assertRefreshAllowed(sessionId: string, ip: string): Promise<void> {
    try {
      const requests = await this.redis.incrementWithTtl(
        `auth:refresh:attempts:${this.identifier(sessionId, ip)}`,
        this.config.refreshRateLimitWindowSeconds,
      );
      if (requests > this.config.refreshRateLimitMax) {
        throw new RateLimitedException(
          'Too many refresh attempts',
          this.config.refreshRateLimitWindowSeconds,
        );
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Persist replay marker for one refresh-token lifetime to fail closed on repeated reuse. */
  public async markRefreshReplay(sessionId: string): Promise<void> {
    try {
      await this.redis.set(
        `auth:refresh:replay:${this.identifier(sessionId)}`,
        '1',
        this.config.refreshTokenTtlSeconds,
      );
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Report whether prior refresh-token reuse already compromised given session. */
  public async isRefreshReplayMarked(sessionId: string): Promise<boolean> {
    try {
      return (await this.redis.get(`auth:refresh:replay:${this.identifier(sessionId)}`)) !== null;
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Derive independent account, IP, and account-plus-IP buckets for distributed credential attacks. */
  private loginIdentifiers(account: string, ip: string): string[] {
    return [
      this.identifier('account', account),
      this.identifier('ip', ip),
      this.identifier('account-ip', account, ip),
    ];
  }

  /** Hash security identifiers before placing them in Redis key space. */
  private identifier(...parts: string[]): string {
    return createHash('sha256').update(parts.join(':')).digest('base64url');
  }

  /** Preserve deliberate throttling errors; fail closed when Redis safeguard is unavailable. */
  private rethrowSecurityError(error: unknown): never {
    if (error instanceof PublicProblemException) {
      throw error;
    }
    throw new PublicProblemException(
      HttpStatus.SERVICE_UNAVAILABLE,
      'dependency-unavailable',
      'Authentication security controls unavailable',
    );
  }
}
