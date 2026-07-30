import { Injectable } from '@nestjs/common';

import { Prisma, type DataOperationSubmission } from '../../generated/prisma/client.js';
import { DataOperationsClient } from '../../data-sync/clients/data-operations.client.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import {
  asRecord,
  requiredArray,
  requiredString,
  type OperationResult,
  type SafeError,
} from './data-operations.types.js';

/** 持续读取 data-sync 权威资源，更新本地 Submission 的非权威结果投影。 */
@Injectable()
export class DataOperationReconcilerService {
  /** 注入本地投影存储与版本化 data-sync 内部查询客户端。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly client: DataOperationsClient,
  ) {}

  /** 对一个有界 accepted 批次进行一次对账；依赖故障保留原投影等待下一轮。 */
  public async reconcileOnce(limit = 20): Promise<number> {
    const submissions = await this.database.client.dataOperationSubmission.findMany({
      where: {
        deliveryStatus: 'ACCEPTED',
        authorityType: { not: null },
        authorityId: { not: null },
        // 终态 Submission 不再轮询；否则旧记录会占满有限批次，饿死仍在运行的操作。
        completedAt: null,
      },
      orderBy: [{ updatedAt: 'asc' }, { id: 'asc' }],
      take: Math.min(Math.max(limit, 1), 100),
    });
    let reconciled = 0;
    for (const submission of submissions) {
      if (await this.reconcileSubmission(submission)) {
        reconciled += 1;
      }
    }
    return reconciled;
  }

  /** 根据 action 与 authorityResource 读取正确的 data-sync 详情并更新结果。 */
  private async reconcileSubmission(submission: DataOperationSubmission): Promise<boolean> {
    if (!submission.authorityType || !submission.authorityId) return false;
    try {
      if (submission.action === 'HEALTH_CHECK_SUBMIT') {
        const detail = await this.client.getHealthCheck(
          { healthCheckId: submission.authorityId },
          submission.requestId,
        );
        const outcome = healthCheckOutcome(detail);
        if (!outcome) return false;
        return this.persistProjection(submission, outcome.result, outcome.completed, null);
      }
      if (submission.action === 'SYNC_CANCEL') {
        const outcome = await this.cancelOutcome(submission);
        if (!outcome) return false;
        return this.persistProjection(submission, outcome.result, outcome.completed, outcome.error);
      }
      if (submission.action === 'SYNC_SUBMIT' || submission.action === 'SYNC_RETRY') {
        const detail = await this.client.getCommand(
          { commandId: submission.authorityId },
          submission.requestId,
        );
        const status = requiredString(detail, 'status');
        if (!status) return false;
        return this.persistProjection(
          submission,
          operationResultFor(status),
          isTerminal(status),
          null,
        );
      }
      // 计划变更由 data-sync 的同步 200 schedule 响应受理即完成，无需重复查询不存在的 detail 路由。
      return this.persistProjection(submission, 'SUCCEEDED', true, null);
    } catch {
      // 下游短暂不可用时不写失败或覆盖 lastObservedAt，下一轮会复用原 authorityResource 对账。
      return false;
    }
  }

  /** 读取被取消的 COMMAND 或 RUN 真正状态，并单独实现 cancel_too_late 语义。 */
  private async cancelOutcome(
    submission: DataOperationSubmission,
  ): Promise<{ result: OperationResult; completed: boolean; error: SafeError | null } | null> {
    if (submission.authorityType === 'COMMAND') {
      const detail = await this.client.getCommand(
        { commandId: submission.authorityId },
        submission.requestId,
      );
      const status = requiredString(detail, 'status');
      return status === null ? null : cancelProjection(status);
    }
    if (submission.authorityType === 'RUN') {
      const detail = await this.client.getRun(
        { runId: submission.authorityId, partitionsCursor: null, timelineCursor: null },
        submission.requestId,
      );
      const run = asRecord(detail.run);
      const status = run === null ? null : requiredString(run, 'status');
      return status === null ? null : cancelProjection(status);
    }
    return null;
  }

  /** 在短事务内写入仅供展示的权威状态投影，并保留 authorityResource 不变。 */
  private async persistProjection(
    submission: DataOperationSubmission,
    result: OperationResult,
    completed: boolean,
    error: SafeError | null,
  ): Promise<boolean> {
    const unchanged =
      submission.operationResult === result &&
      Boolean(submission.completedAt) === completed &&
      JSON.stringify(submission.safeError) === JSON.stringify(error);
    const now = new Date();
    await this.database.client.dataOperationSubmission.update({
      where: { id: submission.id },
      data: {
        operationResult: result,
        safeError: error ?? Prisma.JsonNull,
        completedAt: completed ? now : null,
        lastObservedAt: now,
        ...(unchanged ? {} : { version: { increment: 1 } }),
      },
    });
    return !unchanged;
  }
}

/** 校验健康检查详情的全部有序 target 已可追踪，再将批次状态写入本地投影。 */
function healthCheckOutcome(
  detail: Record<string, unknown>,
): { result: OperationResult; completed: boolean } | null {
  const status = requiredString(detail, 'status');
  const targets = requiredArray(detail, 'targets');
  if (!status || !isHealthCheckStatus(status) || !targets || targets.length === 0) {
    return null;
  }
  let allTargetsTerminal = true;
  for (const target of targets) {
    const outcome = healthCheckTargetOutcome(target);
    if (!outcome) {
      return null;
    }
    if (!isTerminal(outcome.status)) {
      allTargetsTerminal = false;
      continue;
    }
  }
  if (isTerminal(status) && !allTargetsTerminal) {
    // 批次终态与 target 明细不一致时宁可等待下一轮，不能把不完整追踪写成最终事实。
    return null;
  }
  return {
    result: operationResultFor(status),
    completed: isTerminal(status) && allTargetsTerminal,
  };
}

/** 校验单个健康检查 target 的状态专属空值语义，合同漂移时绝不提前完成 Submission。 */
function healthCheckTargetOutcome(value: unknown): { status: string } | null {
  const record = asRecord(value);
  const target = record === null ? null : asRecord(record.target);
  const datasetCode = target === null ? null : requiredString(target, 'datasetCode');
  const targetDataVersion = target === null ? undefined : nullableUuid(target, 'dataVersion');
  const resolvedDataVersion =
    record === null ? undefined : nullableUuid(record, 'resolvedDataVersion');
  const evaluationId = record === null ? undefined : nullableUuid(record, 'evaluationId');
  const error = record === null ? undefined : nullableSafeError(record, 'error');
  const status = record === null ? null : requiredString(record, 'status');
  if (
    !record ||
    !datasetCode ||
    targetDataVersion === undefined ||
    resolvedDataVersion === undefined ||
    evaluationId === undefined ||
    error === undefined ||
    !status ||
    !isHealthCheckTargetStatus(status)
  ) {
    return null;
  }

  if (status === 'QUEUED' || status === 'CANCELLED') {
    return evaluationId === null && error === null ? { status } : null;
  }
  if (status === 'RUNNING') {
    return resolvedDataVersion !== null && evaluationId === null && error === null
      ? { status }
      : null;
  }
  if (status === 'SUCCEEDED') {
    return resolvedDataVersion !== null && evaluationId !== null && error === null
      ? { status }
      : null;
  }
  if (status === 'FAILED') {
    return evaluationId === null && error !== null ? { status } : null;
  }
  // REJECTED 尚未绑定版本，必须保留错误且不能伪造 evaluation。
  return resolvedDataVersion === null && evaluationId === null && error !== null
    ? { status }
    : null;
}

/** 读取一个可空 UUID 字段；字符串形状不符时视为内部合同漂移。 */
function nullableUuid(record: Record<string, unknown>, key: string): string | null | undefined {
  const value = record[key];
  if (value === null) return null;
  return typeof value === 'string' && UUID_PATTERN.test(value) ? value : undefined;
}

/** 读取一个可空、受 ErrorSummary 约束的错误字段，避免原始 Provider 文本穿过对账边界。 */
function nullableSafeError(
  record: Record<string, unknown>,
  key: string,
): SafeError | null | undefined {
  const value = record[key];
  if (value === null) return null;
  const error = asRecord(value);
  const code = error === null ? null : requiredString(error, 'code');
  const stage = error === null ? null : requiredString(error, 'stage');
  const retryable = error === null ? null : error.retryable;
  const message = error === null ? null : requiredString(error, 'message');
  if (
    !code ||
    code.length > 80 ||
    !stage ||
    !ERROR_STAGES.has(stage as SafeError['stage']) ||
    typeof retryable !== 'boolean' ||
    !message ||
    message.length > 500
  ) {
    return undefined;
  }
  return { code, stage: stage as SafeError['stage'], retryable, message };
}

/** 判断批次状态是否属于健康检查合同而非任意 operation 状态。 */
function isHealthCheckStatus(status: string): boolean {
  return HEALTH_CHECK_STATUSES.has(status);
}

/** 判断 target 状态是否属于健康检查结果合同。 */
function isHealthCheckTargetStatus(status: string): boolean {
  return HEALTH_CHECK_TARGET_STATUSES.has(status);
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;
const HEALTH_CHECK_STATUSES = new Set([
  'QUEUED',
  'RUNNING',
  'SUCCEEDED',
  'PARTIAL',
  'FAILED',
  'CANCELLED',
  'REJECTED',
]);
const HEALTH_CHECK_TARGET_STATUSES = new Set([
  'QUEUED',
  'RUNNING',
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'REJECTED',
]);
const ERROR_STAGES = new Set<SafeError['stage']>([
  'PREFLIGHT',
  'QUEUE',
  'DELIVERY',
  'PROVIDER_FETCH',
  'NORMALIZE',
  'QUALITY_GATE',
  'HEALTH_EVALUATION',
  'PERSIST',
  'PUBLISH',
  'CHECKPOINT',
  'SCHEDULE',
  'CANCEL',
  'RECOVERY',
]);

/** 将 data-sync 权威资源状态映射为公开操作结果。 */
function operationResultFor(status: string): OperationResult {
  const allowed: readonly OperationResult[] = [
    'QUEUED',
    'RUNNING',
    'CANCEL_REQUESTED',
    'SUCCEEDED',
    'PARTIAL',
    'FAILED',
    'CANCELLED',
    'INTERRUPTED',
    'SKIPPED',
    'REJECTED',
  ];
  if (!allowed.includes(status as OperationResult)) {
    throw new Error('Unknown data-sync status');
  }
  return status as OperationResult;
}

/** 判断 command、run 或健康检查状态是否已终止。 */
function isTerminal(status: string): boolean {
  // worker 中断后的恢复还可能重新排队或失败，INTERRUPTED 不能停止后续权威对账。
  return ['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED', 'SKIPPED', 'REJECTED'].includes(status);
}

/** 将取消目标真实状态转换为动作结论，不伪造目标最终状态。 */
function cancelProjection(status: string): {
  result: OperationResult;
  completed: boolean;
  error: SafeError | null;
} {
  if (status === 'CANCELLED') {
    return { result: 'CANCELLED', completed: true, error: null };
  }
  if (['SUCCEEDED', 'PARTIAL', 'FAILED', 'SKIPPED', 'REJECTED'].includes(status)) {
    return {
      result: 'FAILED',
      completed: true,
      error: {
        code: 'cancel_too_late',
        stage: 'CANCEL',
        retryable: false,
        message: '取消请求未在目标完成前生效',
      },
    };
  }
  return { result: operationResultFor(status), completed: false, error: null };
}
