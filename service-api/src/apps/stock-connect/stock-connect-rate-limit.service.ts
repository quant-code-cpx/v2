import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { RedisService } from '../../shared/redis/redis.service.js';

/** 表示五条公开查询对应的独立限流桶。 */
export type StockConnectOperation =
  'OVERVIEW' | 'READINESS' | 'CHANNEL' | 'ACTIVE_SECURITIES' | 'SECURITY_CONTEXT';

const RATE_LIMIT_WINDOW_SECONDS = 60;

/** 使用短期 Redis 安全状态限制单用户互联互通读取频率。 */
@Injectable()
export class StockConnectRateLimitService {
  /** 注入全局 Redis 安全状态边界。 */
  public constructor(private readonly redis: RedisService) {}

  /** 消耗一个用户与 operation 隔离的固定窗口配额。 */
  public async assertAllowed(userId: string, operation: StockConnectOperation): Promise<void> {
    try {
      const count = await this.redis.incrementWithTtl(
        `stock-connect:${operation}:${identifier(userId)}`,
        RATE_LIMIT_WINDOW_SECONDS,
      );
      const limit =
        operation === 'OVERVIEW' || operation === 'READINESS' || operation === 'CHANNEL' ? 60 : 120;
      if (count > limit) {
        throw new PublicProblemException(
          HttpStatus.TOO_MANY_REQUESTS,
          'RATE_LIMITED',
          'Stock-connect query rate limit exceeded',
          RATE_LIMIT_WINDOW_SECONDS,
        );
      }
    } catch (error: unknown) {
      if (error instanceof PublicProblemException) throw error;
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'UPSTREAM_UNAVAILABLE',
        'Stock-connect rate limiting is temporarily unavailable',
      );
    }
  }
}

/** 散列用户标识，避免 Redis 键空间直接暴露账号关联信息。 */
function identifier(value: string): string {
  return createHash('sha256').update(value).digest('base64url');
}
