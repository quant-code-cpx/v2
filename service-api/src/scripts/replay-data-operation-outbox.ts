import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { pathToFileURL } from 'node:url';

import { Role, UserStatus } from '../generated/prisma/client.js';
import { AppModule } from '../app.module.js';
import {
  DEAD_LETTER_REPLAY_CONFIRMATION,
  DataOperationOutboxDispatcher,
} from '../apps/data-operations/data-operation-outbox.dispatcher.js';
import { DatabaseService } from '../shared/database/database.service.js';

/** 运行受控 DEAD_LETTER replay，只允许活动超级管理员重投冻结的原 outbox。 */
export async function replayDataOperationOutbox(): Promise<void> {
  const submissionId = requiredEnvironment('DATA_OPERATIONS_REPLAY_SUBMISSION_ID');
  const actorId = requiredEnvironment('DATA_OPERATIONS_REPLAY_ACTOR_ID');
  const confirmation = requiredEnvironment('DATA_OPERATIONS_REPLAY_CONFIRMATION');
  const application = await NestFactory.createApplicationContext(AppModule, { bufferLogs: true });
  const logger = new Logger('DataOperationsReplay');
  application.useLogger(logger);
  try {
    const database = application.get(DatabaseService);
    const dispatcher = application.get(DataOperationOutboxDispatcher);
    const actor = await database.client.user.findUnique({
      where: { id: actorId },
      select: { id: true, role: true, status: true, securityVersion: true },
    });
    // 脚本先校验一遍，dispatcher 会在写事务内再次复验，避免读写之间的权限竞态。
    if (
      !actor ||
      actor.role !== Role.SUPER_ADMIN ||
      actor.status !== UserStatus.ACTIVE ||
      confirmation !== DEAD_LETTER_REPLAY_CONFIRMATION
    ) {
      throw new Error('Dead-letter replay authorization or confirmation is invalid');
    }
    await dispatcher.replayDeadLetter(
      {
        userId: actor.id,
        sessionId: `runbook-replay:${submissionId}`,
        role: actor.role,
        securityVersion: actor.securityVersion,
      },
      submissionId,
      confirmation,
    );
    logger.log(
      JSON.stringify({ operation: 'data-operations-replay', submissionId, status: 'accepted' }),
    );
  } finally {
    await application.close();
  }
}

/** 读取必填 runbook 环境变量，避免误把空值视为确认。 */
function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

/** 判断当前模块是否被 Node 直接作为 runbook 入口执行。 */
function isDirectExecution(): boolean {
  const entry = process.argv[1];
  return entry !== undefined && import.meta.url === pathToFileURL(entry).href;
}

// 直接执行时只输出稳定失败摘要，不能输出 outbox payload、内部 key 或凭据。
if (isDirectExecution()) {
  void replayDataOperationOutbox().catch((error: unknown) => {
    process.stderr.write(
      `${JSON.stringify({
        operation: 'data-operations-replay',
        status: 'failed',
        reason: error instanceof Error ? error.message : 'Data operations replay failed',
      })}\n`,
    );
    process.exitCode = 1;
  });
}
