import { Injectable, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { PrismaPg } from '@prisma/adapter-pg';

import { PrismaClient } from '../../generated/prisma/client.js';
import { AppConfigService } from '../config/app-config.service.js';

@Injectable()
export class DatabaseService implements OnModuleDestroy, OnModuleInit {
  public readonly client: PrismaClient;

  public constructor(config: AppConfigService) {
    const adapter = new PrismaPg({ connectionString: config.databaseUrl });
    this.client = new PrismaClient({ adapter });
  }

  public async onModuleInit(): Promise<void> {
    await this.client.$connect();
  }

  public async onModuleDestroy(): Promise<void> {
    await this.client.$disconnect();
  }

  public async ping(): Promise<void> {
    await this.client.$queryRaw`SELECT 1`;
  }
}
