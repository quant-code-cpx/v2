import { Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';

import type { Environment } from './env.validation.js';

@Injectable()
export class AppConfigService {
  /** Expose validated configuration through typed, centralized accessors. */
  public constructor(private readonly config: ConfigService<Environment, true>) {}

  /** Return validated runtime environment name. */
  public get nodeEnvironment(): Environment['NODE_ENV'] {
    return this.config.getOrThrow('NODE_ENV', { infer: true });
  }

  /** Return HTTP listener port. */
  public get port(): number {
    return this.config.getOrThrow('PORT', { infer: true });
  }

  /** Return versioned API route prefix. */
  public get apiPrefix(): string {
    return this.config.getOrThrow('API_PREFIX', { infer: true });
  }

  /** Return database connection URL for Prisma adapter. */
  public get databaseUrl(): string {
    return this.config.getOrThrow('DATABASE_URL', { infer: true });
  }

  /** Return Redis connection URL for short-lived security state. */
  public get redisUrl(): string {
    return this.config.getOrThrow('REDIS_URL', { infer: true });
  }

  /** Return service namespace prefix for Redis keys. */
  public get redisKeyPrefix(): string {
    return this.config.getOrThrow('REDIS_KEY_PREFIX', { infer: true });
  }

  /** Return expected JWT issuer claim. */
  public get jwtIssuer(): string {
    return this.config.getOrThrow('JWT_ISSUER', { infer: true });
  }

  /** Return expected JWT audience claim. */
  public get jwtAudience(): string {
    return this.config.getOrThrow('JWT_AUDIENCE', { infer: true });
  }

  /** Return validated access-token signing secret. */
  public get jwtAccessSecret(): string {
    return this.config.getOrThrow('JWT_ACCESS_SECRET', { infer: true });
  }

  /** Return access-token lifetime in seconds. */
  public get jwtAccessTtlSeconds(): number {
    return this.config.getOrThrow('JWT_ACCESS_TTL_SECONDS', { infer: true });
  }

  /** Return refresh-session lifetime in seconds. */
  public get refreshTokenTtlSeconds(): number {
    return this.config.getOrThrow('REFRESH_TOKEN_TTL_SECONDS', { infer: true });
  }

  /** Return refresh-cookie SameSite policy. */
  public get cookieSameSite(): 'lax' | 'strict' | 'none' {
    return this.config.getOrThrow('COOKIE_SAME_SITE', { infer: true });
  }

  /** Return whether refresh cookies require HTTPS transport. */
  public get cookieSecure(): boolean {
    return this.config.getOrThrow('COOKIE_SECURE', { infer: true });
  }

  /** Return sole browser origin allowed to send credentialed API requests. */
  public get corsOrigin(): string {
    return this.config.getOrThrow('CORS_ORIGIN', { infer: true });
  }

  /** Return whether upstream proxy headers are trusted for client network identity. */
  public get trustProxy(): boolean {
    return this.config.getOrThrow('TRUST_PROXY', { infer: true });
  }

  /** Return fixed window used to count failed login attempts. */
  public get loginFailureWindowSeconds(): number {
    return this.config.getOrThrow('LOGIN_FAILURE_WINDOW_SECONDS', { infer: true });
  }

  /** Return duration of temporary login lock after failure threshold. */
  public get loginLockSeconds(): number {
    return this.config.getOrThrow('LOGIN_LOCK_SECONDS', { infer: true });
  }

  /** Return failed-login count that triggers temporary lock. */
  public get loginMaxFailures(): number {
    return this.config.getOrThrow('LOGIN_MAX_FAILURES', { infer: true });
  }

  /** Return one CAPTCHA challenge's short security lifetime. */
  public get captchaTtlSeconds(): number {
    return this.config.getOrThrow('CAPTCHA_TTL_SECONDS', { infer: true });
  }

  /** Return fixed window used to limit CAPTCHA issue requests by network identity. */
  public get captchaRateLimitWindowSeconds(): number {
    return this.config.getOrThrow('CAPTCHA_RATE_LIMIT_WINDOW_SECONDS', { infer: true });
  }

  /** Return maximum CAPTCHA issue requests in one network window. */
  public get captchaRateLimitMax(): number {
    return this.config.getOrThrow('CAPTCHA_RATE_LIMIT_MAX', { infer: true });
  }

  /** Return HMAC secret used to store CAPTCHA answers without retaining plaintext. */
  public get captchaHmacSecret(): string {
    return this.config.getOrThrow('CAPTCHA_HMAC_SECRET', { infer: true });
  }

  /** Return fixed window used to count refresh attempts. */
  public get refreshRateLimitWindowSeconds(): number {
    return this.config.getOrThrow('REFRESH_RATE_LIMIT_WINDOW_SECONDS', { infer: true });
  }

  /** Return maximum refresh attempts allowed in one window. */
  public get refreshRateLimitMax(): number {
    return this.config.getOrThrow('REFRESH_RATE_LIMIT_MAX', { infer: true });
  }

  /** Return bounded retry grace period for simultaneous browser refresh requests. */
  public get refreshRaceGraceSeconds(): number {
    return this.config.getOrThrow('REFRESH_RACE_GRACE_SECONDS', { infer: true });
  }

  /** 返回同步服务内部只读 API 的受校验基地址。 */
  public get dataSyncInternalBaseUrl(): string {
    return this.config.getOrThrow('DATA_SYNC_INTERNAL_BASE_URL', { infer: true });
  }

  /** 返回仅用于 API 服务到同步服务的独立 Bearer 凭据。 */
  public get dataSyncInternalBearerToken(): string {
    return this.config.getOrThrow('DATA_SYNC_INTERNAL_BEARER_TOKEN', { infer: true });
  }

  /** 返回下游内部读取请求的有界超时毫秒数。 */
  public get dataSyncInternalRequestTimeoutMs(): number {
    return this.config.getOrThrow('DATA_SYNC_INTERNAL_REQUEST_TIMEOUT_MS', { infer: true });
  }

  /** Return optional one-time super-administrator account from deployment configuration. */
  public get bootstrapAdminAccount(): string | undefined {
    return this.config.get('BOOTSTRAP_ADMIN_ACCOUNT', { infer: true });
  }

  /** Return optional one-time administrator password from deployment configuration. */
  public get bootstrapAdminPassword(): string | undefined {
    return this.config.get('BOOTSTRAP_ADMIN_PASSWORD', { infer: true });
  }
}
