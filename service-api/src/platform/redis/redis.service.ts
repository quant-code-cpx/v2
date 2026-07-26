import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { createClient } from 'redis';

import { AppConfigService } from '../config/app-config.service.js';

@Injectable()
export class RedisService implements OnModuleDestroy, OnModuleInit {
  private readonly logger = new Logger(RedisService.name);
  private readonly client;
  private readonly keyPrefix: string;

  /** Create namespaced Redis client and register connection-error logging. */
  public constructor(config: AppConfigService) {
    this.client = createClient({ url: config.redisUrl });
    this.keyPrefix = config.redisKeyPrefix;
    // Event callback keeps asynchronous client failures observable outside request paths.
    this.client.on('error', (error: unknown) => this.logger.error('Redis client error', error));
  }

  /** Open Redis connection during Nest module initialization. */
  public async onModuleInit(): Promise<void> {
    await this.client.connect();
  }

  /** Gracefully close Redis only when client opened successfully. */
  public async onModuleDestroy(): Promise<void> {
    if (this.client.isOpen) {
      await this.client.quit();
    }
  }

  /** Verify Redis availability for readiness checks. */
  public async ping(): Promise<void> {
    await this.client.ping();
  }

  /** Read one application-namespaced Redis key. */
  public async get(key: string): Promise<string | null> {
    return this.client.get(this.withPrefix(key));
  }

  /** Write one application-namespaced Redis key with mandatory expiry. */
  public async set(key: string, value: string, ttlSeconds: number): Promise<void> {
    await this.client.set(this.withPrefix(key), value, { EX: ttlSeconds });
  }

  /** Remove one application-namespaced Redis key. */
  public async delete(key: string): Promise<void> {
    await this.client.del(this.withPrefix(key));
  }

  /** Increment counter and set expiry only on first observation to preserve fixed window. */
  public async incrementWithTtl(key: string, ttlSeconds: number): Promise<number> {
    const namespacedKey = this.withPrefix(key);
    const result = await this.client.eval(
      // Execute INCR and first-write expiry together so a crash cannot leave an unbounded key.
      'local value = redis.call("INCR", KEYS[1]); if value == 1 then redis.call("EXPIRE", KEYS[1], ARGV[1]); end; return value;',
      { keys: [namespacedKey], arguments: [String(ttlSeconds)] },
    );
    return Number(result);
  }

  /** Atomically consume one short-lived value only when its server-derived digest matches. */
  public async consumeMatchingValue(key: string, expectedValue: string): Promise<boolean> {
    const result = await this.client.eval(
      // Delete on every observed challenge, including a wrong answer, to prevent answer guessing.
      'local value = redis.call("GET", KEYS[1]); if not value then return 0 end; redis.call("DEL", KEYS[1]); if value == ARGV[1] then return 1 end; return 0;',
      { keys: [this.withPrefix(key)], arguments: [expectedValue] },
    );
    return Number(result) === 1;
  }

  /** Prefix local key to isolate this service from other Redis consumers. */
  private withPrefix(key: string): string {
    return `${this.keyPrefix}:${key}`;
  }
}
