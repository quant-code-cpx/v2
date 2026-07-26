import { Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';

import type { Environment } from './env.validation.js';

@Injectable()
export class AppConfigService {
  public constructor(private readonly config: ConfigService<Environment, true>) {}

  public get nodeEnvironment(): Environment['NODE_ENV'] {
    return this.config.getOrThrow('NODE_ENV', { infer: true });
  }

  public get port(): number {
    return this.config.getOrThrow('PORT', { infer: true });
  }

  public get apiPrefix(): string {
    return this.config.getOrThrow('API_PREFIX', { infer: true });
  }

  public get databaseUrl(): string {
    return this.config.getOrThrow('DATABASE_URL', { infer: true });
  }

  public get redisUrl(): string {
    return this.config.getOrThrow('REDIS_URL', { infer: true });
  }

  public get redisKeyPrefix(): string {
    return this.config.getOrThrow('REDIS_KEY_PREFIX', { infer: true });
  }

  public get jwtIssuer(): string {
    return this.config.getOrThrow('JWT_ISSUER', { infer: true });
  }

  public get jwtAudience(): string {
    return this.config.getOrThrow('JWT_AUDIENCE', { infer: true });
  }

  public get jwtAccessSecret(): string {
    return this.config.getOrThrow('JWT_ACCESS_SECRET', { infer: true });
  }

  public get jwtAccessTtlSeconds(): number {
    return this.config.getOrThrow('JWT_ACCESS_TTL_SECONDS', { infer: true });
  }

  public get refreshTokenTtlSeconds(): number {
    return this.config.getOrThrow('REFRESH_TOKEN_TTL_SECONDS', { infer: true });
  }

  public get cookieSameSite(): 'lax' | 'strict' | 'none' {
    return this.config.getOrThrow('COOKIE_SAME_SITE', { infer: true });
  }

  public get cookieSecure(): boolean {
    return this.config.getOrThrow('COOKIE_SECURE', { infer: true });
  }

  public get corsOrigin(): string {
    return this.config.getOrThrow('CORS_ORIGIN', { infer: true });
  }

  public get trustProxy(): boolean {
    return this.config.getOrThrow('TRUST_PROXY', { infer: true });
  }

  public get loginFailureWindowSeconds(): number {
    return this.config.getOrThrow('LOGIN_FAILURE_WINDOW_SECONDS', { infer: true });
  }

  public get loginLockSeconds(): number {
    return this.config.getOrThrow('LOGIN_LOCK_SECONDS', { infer: true });
  }

  public get loginMaxFailures(): number {
    return this.config.getOrThrow('LOGIN_MAX_FAILURES', { infer: true });
  }

  public get refreshRateLimitWindowSeconds(): number {
    return this.config.getOrThrow('REFRESH_RATE_LIMIT_WINDOW_SECONDS', { infer: true });
  }

  public get refreshRateLimitMax(): number {
    return this.config.getOrThrow('REFRESH_RATE_LIMIT_MAX', { infer: true });
  }

  public get bootstrapAdminEmail(): string | undefined {
    return this.config.get('BOOTSTRAP_ADMIN_EMAIL', { infer: true });
  }

  public get bootstrapAdminPassword(): string | undefined {
    return this.config.get('BOOTSTRAP_ADMIN_PASSWORD', { infer: true });
  }
}
