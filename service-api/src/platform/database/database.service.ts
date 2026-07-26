import { Injectable, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { PrismaPg } from '@prisma/adapter-pg';

import { PrismaClient } from '../../generated/prisma/client.js';
import { AppConfigService } from '../config/app-config.service.js';

@Injectable()
export class DatabaseService implements OnModuleDestroy, OnModuleInit {
  public readonly client: PrismaClient;

  /** Build Prisma client using PostgreSQL adapter and validated connection URL. */
  public constructor(config: AppConfigService) {
    const adapter = new PrismaPg({ connectionString: config.databaseUrl });
    this.client = new PrismaClient({ adapter });
  }

  /** Open database connection during Nest module initialization. */
  public async onModuleInit(): Promise<void> {
    await this.client.$connect();
  }

  /** Release database resources during graceful Nest shutdown. */
  public async onModuleDestroy(): Promise<void> {
    await this.client.$disconnect();
  }

  /** Verify database readiness with minimal no-side-effect query. */
  public async ping(): Promise<void> {
    await this.client.$queryRaw`SELECT 1`;
  }
}
