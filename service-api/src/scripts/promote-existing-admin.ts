import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';

import { AppModule } from '../app.module.js';
import { UserService } from '../modules/user/user.service.js';

const PROMOTION_CONFIRMATION = 'PROMOTE_ACTIVE_ADMIN';

/** Promote one explicitly selected ACTIVE ADMIN after the legacy account migration has completed. */
async function promoteExistingAdmin(): Promise<void> {
  const account = process.env.PROMOTE_SUPER_ADMIN_ACCOUNT;
  const confirmation = process.env.PROMOTE_SUPER_ADMIN_CONFIRM;
  if (!account || confirmation !== PROMOTION_CONFIRMATION) {
    throw new Error(
      'PROMOTE_SUPER_ADMIN_ACCOUNT and PROMOTE_SUPER_ADMIN_CONFIRM=PROMOTE_ACTIVE_ADMIN are required',
    );
  }

  const app = await NestFactory.createApplicationContext(AppModule, { bufferLogs: true });
  const logger = new Logger('PromoteExistingAdmin');
  app.useLogger(logger);
  try {
    const user = await app.get(UserService).promoteExistingAdminToSuperAdmin(account);
    logger.log(`Existing administrator promoted to super administrator: ${user.id}`);
  } finally {
    await app.close();
  }
}

// Surface a deterministic nonzero result without logging account input or operational confirmation text.
void promoteExistingAdmin().catch((error: unknown) => {
  const logger = new Logger('PromoteExistingAdmin');
  logger.error(error instanceof Error ? error.message : 'Promotion failed');
  process.exitCode = 1;
});
