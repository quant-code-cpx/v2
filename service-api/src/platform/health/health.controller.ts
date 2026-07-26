import { Controller, Get, ServiceUnavailableException } from '@nestjs/common';
import { ApiExcludeController } from '@nestjs/swagger';

import { DatabaseService } from '../database/database.service.js';
import { RedisService } from '../redis/redis.service.js';

@ApiExcludeController()
@Controller()
export class HealthController {
  /** Inject dependencies required by readiness probe. */
  public constructor(
    private readonly database: DatabaseService,
    private readonly redis: RedisService,
  ) {}

  @Get('health')
  /** Report process liveness without querying external dependencies. */
  public health(): { status: 'ok' } {
    return { status: 'ok' };
  }

  @Get('ready')
  /** Report readiness only when database and Redis are both responsive. */
  public async ready(): Promise<{ status: 'ok' }> {
    try {
      await Promise.all([this.database.ping(), this.redis.ping()]);
      return { status: 'ok' };
    } catch {
      throw new ServiceUnavailableException('Required dependency unavailable');
    }
  }
}
