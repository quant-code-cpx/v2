import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { pathToFileURL } from 'node:url';

import { AppModule } from '../app.module.js';
import { DataOperationOutboxDispatcher } from '../apps/data-operations/data-operation-outbox.dispatcher.js';
import { DataOperationReconcilerService } from '../apps/data-operations/data-operation-reconciler.service.js';

const POLL_INTERVAL_MILLISECONDS = 1_000;

/** 启动独立数据运维 dispatcher 进程，可靠投递 outbox 后持续对账权威资源。 */
export async function runDataOperationsDispatcher(): Promise<void> {
  const application = await NestFactory.createApplicationContext(AppModule, { bufferLogs: true });
  const logger = new Logger('DataOperationsDispatcher');
  application.useLogger(logger);
  const dispatcher = application.get(DataOperationOutboxDispatcher);
  const reconciler = application.get(DataOperationReconcilerService);
  let stopping = false;

  /** 接收编排终止信号后停止领取新 outbox，随后正常关闭数据库和 Redis 连接。 */
  const requestStop = (): void => {
    stopping = true;
  };
  process.once('SIGTERM', requestStop);
  process.once('SIGINT', requestStop);

  try {
    while (!stopping) {
      const dispatched = await dispatcher.dispatchOnce();
      const reconciled = await reconciler.reconcileOnce();
      if (dispatched === 0 && reconciled === 0) {
        await waitForNextPoll();
      }
    }
  } finally {
    process.off('SIGTERM', requestStop);
    process.off('SIGINT', requestStop);
    await application.close();
  }
}

/** 等待有限轮询间隔，以避免空闲 dispatcher 形成无意义的数据库忙循环。 */
function waitForNextPoll(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, POLL_INTERVAL_MILLISECONDS);
  });
}

/** 判断当前模块是否由 Node 直接作为独立 dispatcher 入口执行。 */
function isDirectExecution(): boolean {
  const entry = process.argv[1];
  return entry !== undefined && import.meta.url === pathToFileURL(entry).href;
}

// 仅在直接执行时设置非零退出码，导入该函数的测试不会启动服务基础设施。
if (isDirectExecution()) {
  void runDataOperationsDispatcher().catch((error: unknown) => {
    process.stderr.write(
      `${JSON.stringify({
        operation: 'data-operations-dispatcher',
        status: 'failed',
        reason: error instanceof Error ? error.message : 'Data operations dispatcher failed',
      })}\n`,
    );
    process.exitCode = 1;
  });
}
