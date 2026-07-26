import { Controller, Get, ServiceUnavailableException } from '@nestjs/common';
import { ApiExcludeController } from '@nestjs/swagger';

import { DatabaseService } from '../database/database.service.js';
import { RedisService } from '../redis/redis.service.js';

@ApiExcludeController()
@Controller()
export class HealthController {
  public constructor(
    private readonly database: DatabaseService,
    private readonly redis: RedisService,
  ) {}

  @Get('health')
  public health(): { status: 'ok' } {
    return { status: 'ok' };
  }

  @Get('ready')
  public async ready(): Promise<{ status: 'ok' }> {
    try {
      await Promise.all([this.database.ping(), this.redis.ping()]);
      return { status: 'ok' };
    } catch {
      throw new ServiceUnavailableException('Required dependency unavailable');
    }
  }
}
