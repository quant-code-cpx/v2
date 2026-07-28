import {
  Controller,
  HttpCode,
  HttpStatus,
  Post,
  ServiceUnavailableException,
} from '@nestjs/common';
import { ApiExcludeController } from '@nestjs/swagger';

import { Public } from '../../common/decorators/public.decorator.js';
import { DatabaseService } from '../database/database.service.js';
import { RedisService } from '../redis/redis.service.js';

@ApiExcludeController()
@Public()
@Controller()
export class HealthController {
  /** 注入就绪探针所需的依赖。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly redis: RedisService,
  ) {}

  @Post('health')
  @HttpCode(HttpStatus.OK)
  /** 返回进程存活状态，不查询外部依赖。 */
  public health(): { status: 'ok' } {
    return { status: 'ok' };
  }

  @Post('ready')
  @HttpCode(HttpStatus.OK)
  /** 仅在数据库和 Redis 均可响应时返回就绪状态。 */
  public async ready(): Promise<{ status: 'ok' }> {
    try {
      await Promise.all([this.database.ping(), this.redis.ping()]);
      return { status: 'ok' };
    } catch {
      throw new ServiceUnavailableException('Required dependency unavailable');
    }
  }
}
