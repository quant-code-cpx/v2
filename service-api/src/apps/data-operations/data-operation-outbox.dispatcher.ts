import { HttpStatus, Injectable } from '@nestjs/common';
import { createHmac, randomUUID } from 'node:crypto';

import {
  ApiOutboxState,
  DataOperationDeliveryStatus,
  DataOperationResult,
  Prisma,
  Role,
  UserStatus,
  type ApiOutbox,
  type DataOperationSubmission,
} from '../../generated/prisma/client.js';
import type { AuthContext } from '../../common/models/auth-context.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  DataOperationsClient,
  DataOperationsInternalError,
} from '../../data-sync/clients/data-operations.client.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import {
  asRecord,
  nullableInteger,
  requiredString,
  type AuthorityResource,
  type DataOperationAction,
  type OperationResult,
  type SafeError,
} from './data-operations.types.js';

/** 表示一个已获得短租约、可安全尝试投递的本地 outbox 项。 */
type ClaimedOutbox = ApiOutbox & { submission: DataOperationSubmission };

/** 表示 data-sync 成功受理后可写回 Submission 的最小权威回执。 */
type AcceptedDelivery = {
  authority: AuthorityResource;
  queuePosition: number | null;
  operationResult: OperationResult;
  completed: boolean;
  error: SafeError | null;
};

/** 受控 runbook 必须传入的确认值；它不属于任何浏览器公开 DTO。 */
export const DEAD_LETTER_REPLAY_CONFIRMATION = 'REPLAY_DEAD_LETTER';

/** 使用 PostgreSQL SKIP LOCKED 与租约交付冻结的 data-sync HTTP mutation。 */
@Injectable()
export class DataOperationOutboxDispatcher {
  /** 注入本地账本与唯一允许访问 data-sync 的内部 HTTP 客户端。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly client: DataOperationsClient,
    private readonly config: AppConfigService,
  ) {}

  /** 领取并投递一个有界批次；此方法可由多个独立 dispatcher 进程安全并发调用。 */
  public async dispatchOnce(owner: string = randomUUID(), limit = 20): Promise<number> {
    const claimed = await this.claim(owner, limit);
    for (const outbox of claimed) {
      await this.deliverOne(outbox, owner);
    }
    return claimed.length;
  }

  /** 受控恢复 DEAD_LETTER；只重置原 outbox，不创建 Submission 或新的下游 key。 */
  public async replayDeadLetter(
    actor: AuthContext,
    submissionId: string,
    confirmation: string,
  ): Promise<void> {
    if (actor.role !== Role.SUPER_ADMIN) {
      throw new PublicProblemException(
        HttpStatus.FORBIDDEN,
        'forbidden',
        'Replay requires super-administrator authority',
      );
    }
    if (confirmation !== DEAD_LETTER_REPLAY_CONFIRMATION) {
      throw new PublicProblemException(
        HttpStatus.BAD_REQUEST,
        'replay-confirmation-required',
        'Dead-letter replay requires explicit confirmation',
      );
    }
    try {
      await this.database.client.$transaction(async (transaction) => {
        const currentActor = await transaction.user.findUnique({
          where: { id: actor.userId },
          select: { id: true, role: true, status: true },
        });
        // runbook 必须在写事务中复验用户仍为 ACTIVE SUPER_ADMIN，不能只信任旧 token 快照。
        if (
          !currentActor ||
          currentActor.role !== Role.SUPER_ADMIN ||
          currentActor.status !== UserStatus.ACTIVE
        ) {
          throw new PublicProblemException(
            HttpStatus.FORBIDDEN,
            'forbidden',
            'Replay requires an active super-administrator',
          );
        }
        const outbox = await transaction.apiOutbox.findUnique({
          where: { submissionId },
          include: { submission: true },
        });
        if (!outbox || outbox.state !== ApiOutboxState.DEAD_LETTER) {
          throw new PublicProblemException(
            HttpStatus.CONFLICT,
            'conflict',
            'Submission is not a dead letter',
          );
        }
        const now = new Date();
        await transaction.apiOutbox.update({
          where: { id: outbox.id },
          data: {
            state: ApiOutboxState.PENDING,
            attemptCount: 0,
            leaseOwner: null,
            leaseUntil: null,
            nextAttemptAt: now,
            lastProblemCode: null,
            lastAttemptAt: null,
            deliveredAt: null,
          },
        });
        await transaction.dataOperationSubmission.update({
          where: { id: submissionId },
          data: {
            deliveryStatus: DataOperationDeliveryStatus.PENDING,
            operationResult: DataOperationResult.UNKNOWN,
            safeError: Prisma.JsonNull,
            completedAt: null,
          },
        });
        await transaction.auditLog.create({
          data: {
            actorId: actor.userId,
            action: 'dataops.delivery.replayed',
            targetId: submissionId,
            requestId: outbox.submission.requestId,
            metadata: {
              submissionId,
              replayedBy: currentActor.id,
              replayedAt: now.toISOString(),
              downstreamKeyHmac: this.downstreamKeyHmac(outbox.downstreamIdempotencyKey),
            },
          },
        });
      });
    } catch (error: unknown) {
      if (error instanceof PublicProblemException) throw error;
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        'Data operations storage is unavailable',
      );
    }
  }

  /** 对下游幂等键做服务端 HMAC 摘要，审计可关联但不会泄漏可重放 key。 */
  private downstreamKeyHmac(value: string): string {
    return createHmac('sha256', this.config.jwtAccessSecret).update(value).digest('base64url');
  }

  /** 用 FOR UPDATE SKIP LOCKED 领取未租赁或过期的行，网络请求不在该事务内执行。 */
  private async claim(owner: string, limit: number): Promise<ClaimedOutbox[]> {
    const boundedLimit = Math.min(Math.max(limit, 1), 20);
    try {
      return await this.database.client.$transaction(async (transaction) => {
        const rows = await transaction.$queryRaw<Array<{ id: string }>>`
          WITH candidates AS (
            SELECT "id"
            FROM "api_outboxes"
            WHERE "state" IN ('PENDING'::"ApiOutboxState", 'DELIVERING'::"ApiOutboxState")
              AND "next_attempt_at" <= CURRENT_TIMESTAMP
              AND ("lease_until" IS NULL OR "lease_until" < CURRENT_TIMESTAMP)
            ORDER BY "next_attempt_at" ASC, "id" ASC
            FOR UPDATE SKIP LOCKED
            LIMIT ${boundedLimit}
          )
          UPDATE "api_outboxes" AS outbox
          SET "state" = 'DELIVERING'::"ApiOutboxState",
              "lease_owner" = ${owner},
              "lease_until" = CURRENT_TIMESTAMP + INTERVAL '30 seconds',
              "last_attempt_at" = CURRENT_TIMESTAMP,
              "attempt_count" = outbox."attempt_count" + 1,
              "updated_at" = CURRENT_TIMESTAMP
          FROM candidates
          WHERE outbox."id" = candidates."id"
          RETURNING outbox."id"
        `;
        if (rows.length === 0) return [];
        const items = await transaction.apiOutbox.findMany({
          where: { id: { in: rows.map((row) => row.id) } },
          include: { submission: true },
        });
        await transaction.dataOperationSubmission.updateMany({
          where: { id: { in: items.map((item) => item.submissionId) } },
          data: { deliveryStatus: DataOperationDeliveryStatus.DELIVERING },
        });
        const order = new Map(rows.map((row, index) => [row.id, index]));
        return items.sort((left, right) => (order.get(left.id) ?? 0) - (order.get(right.id) ?? 0));
      });
    } catch {
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        'Data operations storage is unavailable',
      );
    }
  }

  /** 用冻结 body 与同一内部 key 投递一条租约项，并根据安全状态分类处理结果。 */
  private async deliverOne(outbox: ClaimedOutbox, owner: string): Promise<void> {
    try {
      const response = await this.client.deliver(
        outbox.internalPath,
        outbox.canonicalPayload,
        outbox.downstreamIdempotencyKey,
        outbox.submission.requestId,
      );
      const accepted = acceptedDelivery(outbox.submission.action, response);
      await this.markAccepted(outbox, owner, accepted);
    } catch (error: unknown) {
      if (error instanceof DataOperationsInternalError) {
        if ([400, 404, 409, 422].includes(error.status)) {
          await this.markRejected(
            outbox,
            owner,
            rejectionError(outbox.submission.action, error.code),
          );
          return;
        }
        if ([401, 403].includes(error.status)) {
          await this.markDeadLetter(outbox, owner, deliveryError('internal-auth-failed', false));
          return;
        }
        await this.retryOrDeadLetter(
          outbox,
          owner,
          deliveryError(error.code, true),
          error.retryAfter,
        );
        return;
      }
      await this.retryOrDeadLetter(outbox, owner, deliveryError('dependency-unavailable', true));
    }
  }

  /** 在同一短事务内确认 outbox、Submission 与 accepted 审计，且只接受当前租约拥有者。 */
  private async markAccepted(
    outbox: ClaimedOutbox,
    owner: string,
    accepted: AcceptedDelivery,
  ): Promise<void> {
    const now = new Date();
    await this.database.client.$transaction(async (transaction) => {
      const changed = await transaction.apiOutbox.updateMany({
        where: {
          id: outbox.id,
          state: ApiOutboxState.DELIVERING,
          leaseOwner: owner,
          // lease 已到期的 worker 即使刚收到旧响应也不能覆盖新 owner 的交付结果。
          leaseUntil: { gt: now },
        },
        data: {
          state: ApiOutboxState.DELIVERED,
          leaseOwner: null,
          leaseUntil: null,
          deliveredAt: now,
          lastProblemCode: null,
        },
      });
      if (changed.count !== 1) return;
      await transaction.dataOperationSubmission.update({
        where: { id: outbox.submissionId },
        data: {
          deliveryStatus: DataOperationDeliveryStatus.ACCEPTED,
          operationResult: accepted.operationResult,
          authorityType: accepted.authority.resourceType,
          authorityId: accepted.authority.resourceId,
          queuePosition: accepted.queuePosition,
          safeError: accepted.error ?? Prisma.JsonNull,
          completedAt: accepted.completed ? now : null,
          lastObservedAt: now,
          version: { increment: 1 },
        },
      });
      await transaction.auditLog.create({
        data: {
          actorId: outbox.submission.actorId,
          action: 'dataops.delivery.accepted',
          targetId: outbox.submissionId,
          requestId: outbox.submission.requestId,
          metadata: {
            authorityResource: accepted.authority,
            queuePosition: accepted.queuePosition,
            acceptedAt: now.toISOString(),
          },
        },
      });
    });
  }

  /** 在同一短事务内记录权威业务拒绝，而不把首次请求线程的 202 改写为同步失败。 */
  private async markRejected(
    outbox: ClaimedOutbox,
    owner: string,
    error: SafeError,
  ): Promise<void> {
    await this.markTerminal(outbox, owner, {
      deliveryStatus: DataOperationDeliveryStatus.REJECTED,
      operationResult: DataOperationResult.REJECTED,
      error,
      auditAction: 'dataops.delivery.rejected',
      outboxState: ApiOutboxState.DELIVERED,
    });
  }

  /** 将超过尝试上限或内部身份事故的项标记为 DEAD_LETTER，保留原 payload 与 key。 */
  private async markDeadLetter(
    outbox: ClaimedOutbox,
    owner: string,
    error: SafeError,
  ): Promise<void> {
    await this.markTerminal(outbox, owner, {
      deliveryStatus: DataOperationDeliveryStatus.DEAD_LETTER,
      operationResult: DataOperationResult.UNKNOWN,
      error,
      auditAction: 'dataops.delivery.dead_lettered',
      outboxState: ApiOutboxState.DEAD_LETTER,
    });
  }

  /** 将网络、超时或限流失败以相同内部 key 延后重试，达到上限时转为死信。 */
  private async retryOrDeadLetter(
    outbox: ClaimedOutbox,
    owner: string,
    error: SafeError,
    retryAfter?: number,
  ): Promise<void> {
    if (outbox.attemptCount >= 20) {
      await this.markDeadLetter(outbox, owner, error);
      return;
    }
    const delayMilliseconds =
      retryAfter === undefined ? backoffMilliseconds(outbox.attemptCount) : retryAfter * 1_000;
    const nextAttemptAt = new Date(Date.now() + delayMilliseconds);
    const now = new Date();
    await this.database.client.$transaction(async (transaction) => {
      const changed = await transaction.apiOutbox.updateMany({
        where: {
          id: outbox.id,
          state: ApiOutboxState.DELIVERING,
          leaseOwner: owner,
          // 过期 lease 的回退重试也会覆盖已被其他 dispatcher 接管的行，因此必须拒绝。
          leaseUntil: { gt: now },
        },
        data: {
          state: ApiOutboxState.PENDING,
          leaseOwner: null,
          leaseUntil: null,
          nextAttemptAt,
          lastProblemCode: error.code,
        },
      });
      if (changed.count !== 1) return;
      await transaction.dataOperationSubmission.update({
        where: { id: outbox.submissionId },
        data: {
          deliveryStatus: DataOperationDeliveryStatus.PENDING,
          operationResult: DataOperationResult.UNKNOWN,
          safeError: Prisma.JsonNull,
          authorityType: null,
          authorityId: null,
          queuePosition: null,
        },
      });
    });
  }

  /** 原子写入 outbox 终态、Submission 终态与安全交付审计。 */
  private async markTerminal(
    outbox: ClaimedOutbox,
    owner: string,
    outcome: {
      deliveryStatus: DataOperationDeliveryStatus;
      operationResult: DataOperationResult;
      error: SafeError;
      auditAction: string;
      outboxState: ApiOutboxState;
    },
  ): Promise<void> {
    const now = new Date();
    await this.database.client.$transaction(async (transaction) => {
      const changed = await transaction.apiOutbox.updateMany({
        where: {
          id: outbox.id,
          state: ApiOutboxState.DELIVERING,
          leaseOwner: owner,
          // 终态写回同样受租约保护，防止 stale worker 写死信或业务拒绝。
          leaseUntil: { gt: now },
        },
        data: {
          state: outcome.outboxState,
          leaseOwner: null,
          leaseUntil: null,
          deliveredAt: now,
          lastProblemCode: outcome.error.code,
        },
      });
      if (changed.count !== 1) return;
      await transaction.dataOperationSubmission.update({
        where: { id: outbox.submissionId },
        data: {
          deliveryStatus: outcome.deliveryStatus,
          operationResult: outcome.operationResult,
          authorityType: null,
          authorityId: null,
          queuePosition: null,
          safeError: outcome.error,
          completedAt: now,
          // DEAD_LETTER 尚未取得下游权威状态，合同要求 lastObservedAt 保持 null。
          lastObservedAt:
            outcome.deliveryStatus === DataOperationDeliveryStatus.DEAD_LETTER ? null : now,
          version: { increment: 1 },
        },
      });
      await transaction.auditLog.create({
        data: {
          actorId: outbox.submission.actorId,
          action: outcome.auditAction,
          targetId: outbox.submissionId,
          requestId: outbox.submission.requestId,
          metadata: {
            code: outcome.error.code,
            ...(outcome.deliveryStatus === DataOperationDeliveryStatus.DEAD_LETTER
              ? {
                  attemptCount: outbox.attemptCount,
                  lastProblemCode: outcome.error.code,
                  lastAttemptAt: outbox.lastAttemptAt?.toISOString() ?? null,
                }
              : {}),
          },
        },
      });
    });
  }
}

/** 从不同 action 的 data-sync 成功响应提取固定 authorityResource 映射。 */
function acceptedDelivery(action: DataOperationAction, value: unknown): AcceptedDelivery {
  const record = asRecord(value);
  if (!record) throw new Error('accepted response is not an object');
  if (action === 'SYNC_SUBMIT' || action === 'SYNC_RETRY') {
    const commandId = requiredString(record, 'commandId');
    const queuePosition = nullableInteger(record, 'queuePosition');
    const status = requiredString(record, 'status');
    if (!commandId || queuePosition === undefined || !status)
      throw new Error('invalid command receipt');
    return {
      authority: { resourceType: 'COMMAND', resourceId: commandId },
      queuePosition,
      operationResult: operationResultFor(status),
      completed: isTerminalResult(status),
      error: null,
    };
  }
  if (action === 'SYNC_CANCEL') {
    const target = asRecord(record.target);
    const resourceType = target === null ? null : requiredString(target, 'resourceType');
    const resourceId = target === null ? null : requiredString(target, 'resourceId');
    const targetStatus = requiredString(record, 'targetStatus');
    if (
      !resourceType ||
      !resourceId ||
      !targetStatus ||
      !['COMMAND', 'RUN'].includes(resourceType)
    ) {
      throw new Error('invalid cancel receipt');
    }
    const outcome = cancelAcceptedProjection(targetStatus);
    return {
      authority: { resourceType: resourceType as 'COMMAND' | 'RUN', resourceId },
      queuePosition: null,
      operationResult: outcome.operationResult,
      completed: outcome.completed,
      error: outcome.error,
    };
  }
  if (action === 'HEALTH_CHECK_SUBMIT') {
    const healthCheckId = requiredString(record, 'healthCheckId');
    const status = requiredString(record, 'status');
    if (!healthCheckId || !status) throw new Error('invalid health check receipt');
    return {
      authority: { resourceType: 'HEALTH_CHECK', resourceId: healthCheckId },
      queuePosition: null,
      operationResult: operationResultFor(status),
      completed: isTerminalResult(status),
      error: null,
    };
  }
  const summary = asRecord(record.summary);
  const scheduleId = summary === null ? null : requiredString(summary, 'scheduleId');
  if (!scheduleId) throw new Error('invalid schedule receipt');
  return {
    authority: { resourceType: 'SCHEDULE', resourceId: scheduleId },
    queuePosition: null,
    operationResult: 'SUCCEEDED',
    completed: true,
    error: null,
  };
}

/** 将取消受理回执中的目标状态转换为动作结论，终态非取消即为 cancel_too_late。 */
function cancelAcceptedProjection(status: string): {
  operationResult: OperationResult;
  completed: boolean;
  error: SafeError | null;
} {
  if (status === 'CANCELLED') {
    return { operationResult: 'CANCELLED', completed: true, error: null };
  }
  if (isTerminalResult(status)) {
    return {
      operationResult: 'FAILED',
      completed: true,
      error: {
        code: 'cancel_too_late',
        stage: 'CANCEL',
        retryable: false,
        message: '取消请求未在目标完成前生效',
      },
    };
  }
  return { operationResult: operationResultFor(status), completed: false, error: null };
}

/** 将权威资源状态映射为操作结果；未知值视为合同漂移而非猜测成功。 */
function operationResultFor(status: string): OperationResult {
  const values: readonly OperationResult[] = [
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
  if (!values.includes(status as OperationResult)) {
    throw new Error('unknown operation status');
  }
  return status as OperationResult;
}

/** 判断权威命令、运行或健康检查状态是否已经终止。 */
function isTerminalResult(status: string): boolean {
  // INTERRUPTED 是恢复窗口中的中间态，后续仍可回到 QUEUED 或 FAILED，不能完成动作投影。
  return ['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED', 'SKIPPED', 'REJECTED'].includes(status);
}

/** 将 data-sync 业务拒绝归一为不包含 Provider 正文的稳定 ErrorSummary。 */
function rejectionError(action: string, code: string): SafeError {
  return {
    code: code.slice(0, 80),
    stage: stageFor(action),
    retryable: false,
    message: '数据运维请求未被权威服务受理',
  };
}

/** 将可重试投递错误归一为安全 ErrorSummary。 */
function deliveryError(code: string, retryable: boolean): SafeError {
  return {
    code: code.slice(0, 80),
    stage: 'DELIVERY',
    retryable,
    message: retryable ? '数据运维请求等待重新投递' : '数据运维内部身份不可用',
  };
}

/** 按动作选择与合同一致的失败阶段。 */
function stageFor(action: string): SafeError['stage'] {
  if (action === 'SYNC_CANCEL') return 'CANCEL';
  if (action === 'HEALTH_CHECK_SUBMIT') return 'HEALTH_EVALUATION';
  if (action.startsWith('SCHEDULE_')) return 'SCHEDULE';
  return 'QUEUE';
}

/** 计算 1 秒起步、5 分钟封顶的 full-jitter 指数退避。 */
function backoffMilliseconds(attemptCount: number): number {
  const ceiling = Math.min(300_000, 1_000 * 2 ** Math.max(attemptCount - 1, 0));
  return Math.max(1_000, Math.floor(Math.random() * ceiling));
}
