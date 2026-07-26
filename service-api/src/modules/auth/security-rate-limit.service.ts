import { HttpException, HttpStatus, Injectable, ServiceUnavailableException } from '@nestjs/common';
import { createHash } from 'node:crypto';

import { AppConfigService } from '../../platform/config/app-config.service.js';
import { RedisService } from '../../platform/redis/redis.service.js';

class RateLimitedException extends HttpException {
  /** Create service-specific HTTP 429 response without exposing internal rate-limit state. */
  public constructor(message: string) {
    super(message, HttpStatus.TOO_MANY_REQUESTS);
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
  public async assertLoginAllowed(email: string, ip: string): Promise<void> {
    const lockKey = this.loginLockKey(email, ip);
    try {
      if (await this.redis.get(lockKey)) {
        throw new RateLimitedException('Too many login attempts');
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Count failed login, then atomically convert threshold breach into temporary lock. */
  public async recordFailedLogin(email: string, ip: string): Promise<void> {
    const identifier = this.identifier(email, ip);
    try {
      const attempts = await this.redis.incrementWithTtl(
        `auth:login:failed:${identifier}`,
        this.config.loginFailureWindowSeconds,
      );
      // Delete counter once locked so another request cannot extend failure window unexpectedly.
      if (attempts >= this.config.loginMaxFailures) {
        await this.redis.set(`auth:login:locked:${identifier}`, '1', this.config.loginLockSeconds);
        await this.redis.delete(`auth:login:failed:${identifier}`);
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  /** Remove prior login-failure counter after verified successful authentication. */
  public async resetLoginFailures(email: string, ip: string): Promise<void> {
    try {
      await this.redis.delete(`auth:login:failed:${this.identifier(email, ip)}`);
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
        throw new RateLimitedException('Too many refresh attempts');
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

  /** Construct lock key from opaque hashed login identity. */
  private loginLockKey(email: string, ip: string): string {
    return `auth:login:locked:${this.identifier(email, ip)}`;
  }

  /** Hash security identifiers before placing them in Redis key space. */
  private identifier(...parts: string[]): string {
    return createHash('sha256').update(parts.join(':')).digest('base64url');
  }

  /** Preserve deliberate throttling errors; fail closed when Redis safeguard is unavailable. */
  private rethrowSecurityError(error: unknown): never {
    if (error instanceof RateLimitedException) {
      throw error;
    }
    throw new ServiceUnavailableException('Authentication security controls unavailable');
  }
}
