import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { createClient } from 'redis';

import { AppConfigService } from '../config/app-config.service.js';

@Injectable()
export class RedisService implements OnModuleDestroy, OnModuleInit {
  private readonly logger = new Logger(RedisService.name);
  private readonly client;
  private readonly keyPrefix: string;

  public constructor(config: AppConfigService) {
    this.client = createClient({ url: config.redisUrl });
    this.keyPrefix = config.redisKeyPrefix;
    this.client.on('error', (error: unknown) => this.logger.error('Redis client error', error));
  }

  public async onModuleInit(): Promise<void> {
    await this.client.connect();
  }

  public async onModuleDestroy(): Promise<void> {
    if (this.client.isOpen) {
      await this.client.quit();
    }
  }

  public async ping(): Promise<void> {
    await this.client.ping();
  }

  public async get(key: string): Promise<string | null> {
    return this.client.get(this.withPrefix(key));
  }

  public async set(key: string, value: string, ttlSeconds: number): Promise<void> {
    await this.client.set(this.withPrefix(key), value, { EX: ttlSeconds });
  }

  public async delete(key: string): Promise<void> {
    await this.client.del(this.withPrefix(key));
  }

  public async incrementWithTtl(key: string, ttlSeconds: number): Promise<number> {
    const namespacedKey = this.withPrefix(key);
    const value = await this.client.incr(namespacedKey);
    await this.client.expire(namespacedKey, ttlSeconds, 'NX');
    return value;
  }

  private withPrefix(key: string): string {
    return `${this.keyPrefix}:${key}`;
  }
}
