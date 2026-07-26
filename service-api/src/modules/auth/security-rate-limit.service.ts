import { HttpException, HttpStatus, Injectable, ServiceUnavailableException } from '@nestjs/common';
import { createHash } from 'node:crypto';

import { AppConfigService } from '../../platform/config/app-config.service.js';
import { RedisService } from '../../platform/redis/redis.service.js';

class RateLimitedException extends HttpException {
  public constructor(message: string) {
    super(message, HttpStatus.TOO_MANY_REQUESTS);
  }
}

@Injectable()
export class SecurityRateLimitService {
  public constructor(
    private readonly redis: RedisService,
    private readonly config: AppConfigService,
  ) {}

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

  public async recordFailedLogin(email: string, ip: string): Promise<void> {
    const identifier = this.identifier(email, ip);
    try {
      const attempts = await this.redis.incrementWithTtl(
        `auth:login:failed:${identifier}`,
        this.config.loginFailureWindowSeconds,
      );
      if (attempts >= this.config.loginMaxFailures) {
        await this.redis.set(`auth:login:locked:${identifier}`, '1', this.config.loginLockSeconds);
        await this.redis.delete(`auth:login:failed:${identifier}`);
      }
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  public async resetLoginFailures(email: string, ip: string): Promise<void> {
    try {
      await this.redis.delete(`auth:login:failed:${this.identifier(email, ip)}`);
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

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

  public async isRefreshReplayMarked(sessionId: string): Promise<boolean> {
    try {
      return (await this.redis.get(`auth:refresh:replay:${this.identifier(sessionId)}`)) !== null;
    } catch (error: unknown) {
      this.rethrowSecurityError(error);
    }
  }

  private loginLockKey(email: string, ip: string): string {
    return `auth:login:locked:${this.identifier(email, ip)}`;
  }

  private identifier(...parts: string[]): string {
    return createHash('sha256').update(parts.join(':')).digest('base64url');
  }

  private rethrowSecurityError(error: unknown): never {
    if (error instanceof RateLimitedException) {
      throw error;
    }
    throw new ServiceUnavailableException('Authentication security controls unavailable');
  }
}
