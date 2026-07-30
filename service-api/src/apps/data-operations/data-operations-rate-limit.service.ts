import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { RedisService } from '../../shared/redis/redis.service.js';
import type { DataOperationAction } from './data-operations.types.js';

/** 为数据运维主动操作提供 Redis 短期防滥用计数，不保存任何业务权威状态。 */
@Injectable()
export class DataOperationsRateLimitService {
  /** 注入短期 Redis 安全状态服务。 */
  public constructor(private readonly redis: RedisService) {}

  /** 对写操作施加每 actor、每 action 的一分钟固定窗口，并在 Redis 故障时拒绝。 */
  public async assertWriteAllowed(actorId: string, action: DataOperationAction): Promise<void> {
    await this.assertAllowed(`write:${action}:${this.identifier(actorId)}`, 10, 60);
  }

  /** 对可能消耗 data-sync 预算的预检施加每 actor 一分钟的独立上限。 */
  public async assertPreflightAllowed(actorId: string): Promise<void> {
    await this.assertAllowed(`preflight:${this.identifier(actorId)}`, 30, 60);
  }

  /** 原子增加一个命名空间内的短期计数，任何 Redis 异常都 fail closed。 */
  private async assertAllowed(key: string, maximum: number, ttlSeconds: number): Promise<void> {
    try {
      const count = await this.redis.incrementWithTtl(`dataops:${key}`, ttlSeconds);
      if (count > maximum) {
        throw new PublicProblemException(
          HttpStatus.TOO_MANY_REQUESTS,
          'rate-limited',
          'Data operations request is rate limited',
          ttlSeconds,
        );
      }
    } catch (error: unknown) {
      if (error instanceof PublicProblemException) {
        throw error;
      }
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        'Data operations security controls unavailable',
      );
    }
  }

  /** 对 actor 标识哈希后写入 Redis key，避免把 UUID 直接暴露给运维键空间。 */
  private identifier(actorId: string): string {
    return createHash('sha256').update(actorId).digest('base64url');
  }
}
