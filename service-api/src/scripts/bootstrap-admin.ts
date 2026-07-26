import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';

import { AppModule } from '../app.module.js';
import { Role } from '../generated/prisma/client.js';
import { UserService } from '../modules/user/user.service.js';
import { AppConfigService } from '../platform/config/app-config.service.js';

async function bootstrapAdmin(): Promise<void> {
  const app = await NestFactory.createApplicationContext(AppModule, { bufferLogs: true });
  const logger = new Logger('BootstrapAdmin');
  app.useLogger(logger);
  try {
    const config = app.get(AppConfigService);
    const users = app.get(UserService);
    const email = config.bootstrapAdminEmail;
    const password = config.bootstrapAdminPassword;
    if (!email || !password) {
      throw new Error('BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD are required');
    }
    if (await users.hasUsers()) {
      throw new Error('Refusing bootstrap: users already exist');
    }
    const user = await users.createUser(
      {
        email,
        displayName: 'Administrator',
        password,
        role: Role.ADMIN,
      },
      { actorId: null },
    );
    logger.log(`Bootstrap administrator created: ${user.id}`);
  } finally {
    await app.close();
  }
}

void bootstrapAdmin();
