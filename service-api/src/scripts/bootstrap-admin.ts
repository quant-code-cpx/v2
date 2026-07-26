import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';

import { AppModule } from '../app.module.js';
import { UserService } from '../modules/user/user.service.js';
import { AppConfigService } from '../platform/config/app-config.service.js';

/** 在迁移完成后确认唯一 `SUPER_ADMIN` 存在，同时绝不输出账号或密码。 */
async function bootstrapAdmin(): Promise<void> {
  const app = await NestFactory.createApplicationContext(AppModule, { bufferLogs: true });
  const logger = new Logger('BootstrapAdmin');
  app.useLogger(logger);
  try {
    const config = app.get(AppConfigService);
    const users = app.get(UserService);
    const account = config.bootstrapAdminAccount;
    const password = config.bootstrapAdminPassword;
    const result = await users.ensureBootstrapSuperAdmin(account, password);
    logger.log(
      result.created
        ? 'Bootstrap super administrator created'
        : 'Active super administrator already exists; bootstrap skipped',
    );
  } finally {
    await app.close();
  }
}

// 仅输出确定的失败状态，不记录初始化凭证。
void bootstrapAdmin().catch((error: unknown) => {
  const logger = new Logger('BootstrapAdmin');
  logger.error(error instanceof Error ? error.message : 'Bootstrap failed');
  process.exitCode = 1;
});
