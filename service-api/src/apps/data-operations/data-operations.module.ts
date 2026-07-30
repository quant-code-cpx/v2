import { Module } from '@nestjs/common';

import { DataSyncModule } from '../../data-sync/data-sync.module.js';
import { DataOperationOutboxDispatcher } from './data-operation-outbox.dispatcher.js';
import { DataOperationReconcilerService } from './data-operation-reconciler.service.js';
import { DataOperationSubmissionService } from './data-operation-submission.service.js';
import { DataOperationsController } from './data-operations.controller.js';
import { DataOperationsProjectionService } from './data-operations-projection.service.js';
import { DataOperationsQueryService } from './data-operations-query.service.js';
import { DataOperationsRateLimitService } from './data-operations-rate-limit.service.js';

/** 装配数据运维公开 API、可靠 outbox 与 data-sync 权威投影，绝不持有同步事实。 */
@Module({
  imports: [DataSyncModule],
  controllers: [DataOperationsController],
  providers: [
    DataOperationSubmissionService,
    DataOperationsRateLimitService,
    DataOperationsProjectionService,
    DataOperationsQueryService,
    DataOperationOutboxDispatcher,
    DataOperationReconcilerService,
  ],
  exports: [DataOperationOutboxDispatcher, DataOperationReconcilerService],
})
export class DataOperationsModule {}
