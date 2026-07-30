import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { RedisService } from '../../shared/redis/redis.service.js';
import type { EquitySearchRequestDto } from './dto/equity-workspace.dto.js';

/** 为已认证股票中心读取提供短期 Redis 防滥用计数。 */
@Injectable()
export class EquityWorkspaceRateLimitService {
  /** 注入仅保存短期安全状态的 Redis 服务。 */
  public constructor(private readonly redis: RedisService) {}

  /** 对搜索施加每用户一分钟基础桶，并为复杂多条件搜索增加独立成本桶。 */
  public async assertSearchAllowed(userId: string, input: EquitySearchRequestDto): Promise<void> {
    const actor = identifier(userId);
    await this.assertAllowed(`search:${actor}`, 60);
    if (isComplexSearch(input)) {
      await this.assertAllowed(`search-complex:${actor}`, 20);
    }
  }

  /** 对事件事实读取施加每用户一分钟一百二十次上限。 */
  public async assertEventsAllowed(userId: string): Promise<void> {
    await this.assertAllowed(`events:${identifier(userId)}`, 120);
  }

  /** 对详情数据状态读取施加每用户一分钟六十次上限。 */
  public async assertDataStatusAllowed(userId: string): Promise<void> {
    await this.assertAllowed(`data-status:${identifier(userId)}`, 60);
  }

  /** 原子增加一个短期计数，并在 Redis 故障时拒绝以免绕过防滥用边界。 */
  private async assertAllowed(key: string, maximum: number): Promise<void> {
    try {
      const count = await this.redis.incrementWithTtl(`equity-workspace:${key}`, 60);
      if (count > maximum) {
        throw new PublicProblemException(
          HttpStatus.TOO_MANY_REQUESTS,
          'rate-limited',
          'Equity workspace request is rate limited',
          60,
        );
      }
    } catch (error: unknown) {
      if (error instanceof PublicProblemException) throw error;
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        'Equity workspace security controls unavailable',
      );
    }
  }
}

/** 对用户 UUID 做不可逆摘要，避免把认证标识直接写入 Redis key。 */
function identifier(userId: string): string {
  return createHash('sha256').update(userId).digest('base64url');
}

/** 识别会显著增加索引扫描或多组件谓词成本的搜索。 */
function isComplexSearch(input: EquitySearchRequestDto): boolean {
  return (
    input.memberships !== undefined ||
    input.valuation !== undefined ||
    input.moneyFlow !== undefined ||
    (input.sort?.length ?? 0) > 1
  );
}
