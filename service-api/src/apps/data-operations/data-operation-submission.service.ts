import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash, createHmac, randomUUID } from 'node:crypto';

import {
  DataOperationAction as PrismaDataOperationAction,
  DataOperationResult as PrismaDataOperationResult,
  DataOperationDeliveryStatus as PrismaDeliveryStatus,
  Role,
  type DataOperationSubmission,
  type Prisma,
} from '../../generated/prisma/client.js';
import type { AuthContext } from '../../common/models/auth-context.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import type { DataOperationsWriteRequest } from '../../data-sync/contracts/data-operations.contract.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import {
  DATA_OPERATION_INTERNAL_PATH,
  asRecord,
  canonicalJson,
  iso,
  type DataOperationAction,
  type SafeError,
  type SubmissionReceipt,
} from './data-operations.types.js';

/** 在同一数据库事务中持久化授权意图、冻结 outbox 与安全审计。 */
@Injectable()
export class DataOperationSubmissionService {
  /** 注入 API PostgreSQL 权威存储和用于生成不透明 actorRef 的服务端密钥。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly config: AppConfigService,
  ) {}

  /** 创建或复用一个公开幂等 Submission；请求线程绝不调用 data-sync。 */
  public async submit(
    actor: AuthContext,
    action: DataOperationAction,
    request: DataOperationsWriteRequest,
    idempotencyKey: string,
    requestId: string,
  ): Promise<SubmissionReceipt> {
    this.assertWriter(actor);
    const requestHash = this.requestHash(action, request);
    const existing = await this.findByIdempotency(actor.userId, idempotencyKey);
    if (existing) {
      return this.resolveIdempotent(existing, requestHash);
    }

    const actorRef = this.actorRef(actor.userId);
    const reason = request.reason;
    const submissionId = randomUUID();
    const canonicalPayload = this.internalPayload(
      action,
      submissionId,
      actorRef,
      actor.role,
      request,
    );
    this.assertOutboxPayloadSize(canonicalPayload);
    try {
      const submission = await this.database.client.$transaction(async (transaction) => {
        const created = await transaction.dataOperationSubmission.create({
          data: {
            id: submissionId,
            actorId: actor.userId,
            actorRole: actor.role,
            action,
            idempotencyKey,
            requestHash,
            sanitizedRequest: request,
            actorRef,
            reason,
            requestId,
          },
        });
        // 下游幂等键从不可变 submissionId 派生，绝不透传浏览器提供的公开 key。
        await transaction.apiOutbox.create({
          data: {
            submissionId: created.id,
            downstreamIdempotencyKey: `dataops:${created.id}`,
            internalPath: DATA_OPERATION_INTERNAL_PATH[action],
            canonicalPayload: prismaJsonObject(canonicalPayload),
          },
        });
        // 审计写入与 Submission/Outbox 同事务，避免出现已授权但无证据的主动操作。
        await transaction.auditLog.create({
          data: {
            actorId: actor.userId,
            action: 'dataops.request.authorized',
            targetId: created.id,
            requestId,
            metadata: {
              action,
              actorRole: actor.role,
              reason,
              targetSummary: this.targetSummary(action, request),
            },
          },
        });
        return created;
      });
      return this.receipt(submission);
    } catch (error: unknown) {
      const concurrent = await this.findByIdempotency(actor.userId, idempotencyKey);
      if (concurrent) {
        return this.resolveIdempotent(concurrent, requestHash);
      }
      if (error instanceof PublicProblemException) {
        throw error;
      }
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        'Data operations storage is unavailable',
      );
    }
  }

  /** 读取一个本地 Submission 收据，不将内部 outbox 字段暴露给浏览器。 */
  public async getReceipt(submissionId: string): Promise<SubmissionReceipt> {
    let submission: DataOperationSubmission | null;
    try {
      submission = await this.database.client.dataOperationSubmission.findUnique({
        where: { id: submissionId },
      });
    } catch {
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        'Data operations storage is unavailable',
      );
    }
    if (!submission) {
      throw new PublicProblemException(
        HttpStatus.NOT_FOUND,
        'not-found',
        'Data operations submission is not found',
      );
    }
    return this.receipt(submission);
  }

  /** 为投影服务按 requestId 查询本地 Submission，不公开原始 JSONB。 */
  public async findByRequestIds(requestIds: readonly string[]): Promise<DataOperationSubmission[]> {
    if (requestIds.length === 0) {
      return [];
    }
    return this.database.client.dataOperationSubmission.findMany({
      where: { requestId: { in: [...new Set(requestIds)] } },
    });
  }

  /** 为 actorRef 投影查询用户提交记录，actorRef 本身不会返回浏览器。 */
  public async findByActorRefs(actorRefs: readonly string[]): Promise<DataOperationSubmission[]> {
    if (actorRefs.length === 0) {
      return [];
    }
    return this.database.client.dataOperationSubmission.findMany({
      where: { actorRef: { in: [...new Set(actorRefs)] } },
      include: { actor: true },
    });
  }

  /** 读取操作记录页所需的本地用户 Submission，SYSTEM 事件由 data-sync 单独投影。 */
  public async searchSubmissions(input: {
    actorIds?: string[] | undefined;
    actions?: string[] | undefined;
    deliveryStatuses?: string[] | undefined;
    operationResults?: string[] | undefined;
    occurredFrom?: string | null | undefined;
    occurredTo?: string | null | undefined;
    cursor?: { authorizedAt: Date; id: string } | null | undefined;
    limit?: number | undefined;
  }): Promise<
    Array<
      DataOperationSubmission & {
        actor: { id: string; displayName: string; status: string } | null;
      }
    >
  > {
    const limit = input.limit ?? 50;
    const actions = input.actions?.filter((value): value is PrismaDataOperationAction =>
      Object.values(PrismaDataOperationAction).includes(value as PrismaDataOperationAction),
    );
    const deliveryStatuses = input.deliveryStatuses?.filter(
      (value): value is PrismaDeliveryStatus =>
        Object.values(PrismaDeliveryStatus).includes(value as PrismaDeliveryStatus),
    );
    const operationResults = input.operationResults?.filter(
      (value): value is PrismaDataOperationResult =>
        Object.values(PrismaDataOperationResult).includes(value as PrismaDataOperationResult),
    );
    const impossibleFilter =
      (input.actions !== undefined && actions?.length === 0) ||
      (input.deliveryStatuses !== undefined && deliveryStatuses?.length === 0) ||
      (input.operationResults !== undefined && operationResults?.length === 0);
    const where: Prisma.DataOperationSubmissionWhereInput = {
      ...(impossibleFilter ? { id: { in: [] } } : {}),
      ...(input.actorIds === undefined ? {} : { actorId: { in: input.actorIds } }),
      ...(input.actions === undefined ? {} : { action: { in: actions ?? [] } }),
      ...(input.deliveryStatuses === undefined
        ? {}
        : { deliveryStatus: { in: deliveryStatuses ?? [] } }),
      ...(input.operationResults === undefined
        ? {}
        : { operationResult: { in: operationResults ?? [] } }),
      ...dateWindow(input.occurredFrom, input.occurredTo),
    };
    if (input.cursor) {
      // 相同毫秒内的 UUID 次序是联合游标的一部分，避免翻页时丢失或重复 Submission。
      where.AND = [
        {
          OR: [
            { authorizedAt: { lt: input.cursor.authorizedAt } },
            { authorizedAt: input.cursor.authorizedAt, id: { lt: input.cursor.id } },
          ],
        },
      ];
    }
    return this.database.client.dataOperationSubmission.findMany({
      where,
      include: { actor: { select: { id: true, displayName: true, status: true } } },
      orderBy: [{ authorizedAt: 'desc' }, { id: 'desc' }],
      take: limit,
    });
  }

  /** 将数据库行映射为合同收据，并保留状态机规定的 null 语义。 */
  public receipt(submission: DataOperationSubmission): SubmissionReceipt {
    const authorityResource =
      submission.authorityType === null || submission.authorityId === null
        ? null
        : {
            resourceType: submission.authorityType,
            resourceId: submission.authorityId,
          };
    return {
      submissionId: submission.id,
      action: submission.action,
      deliveryStatus: submission.deliveryStatus,
      operationResult: submission.operationResult,
      authorityResource,
      queuePosition: submission.queuePosition,
      authorizedAt: iso(submission.authorizedAt),
      updatedAt: iso(submission.updatedAt),
      requestId: submission.requestId,
      error: this.safeError(submission.safeError),
    };
  }

  /** 按 actor 与公开 key 查询已持久化的幂等请求。 */
  private async findByIdempotency(
    actorId: string,
    idempotencyKey: string,
  ): Promise<DataOperationSubmission | null> {
    try {
      return await this.database.client.dataOperationSubmission.findUnique({
        where: { actorId_idempotencyKey: { actorId, idempotencyKey } },
      });
    } catch {
      throw new PublicProblemException(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        'Data operations storage is unavailable',
      );
    }
  }

  /** 根据稳定请求摘要决定返回既有收据或拒绝不同语义的 key 复用。 */
  private resolveIdempotent(
    submission: DataOperationSubmission,
    requestHash: string,
  ): SubmissionReceipt {
    if (submission.requestHash !== requestHash) {
      throw new PublicProblemException(
        HttpStatus.CONFLICT,
        'idempotency-key-reused',
        'Idempotency-Key was reused with a different request',
      );
    }
    return this.receipt(submission);
  }

  /** 生成包含 action 和规范 JSON body 的 SHA-256 摘要，防止跨路由 key 混用。 */
  private requestHash(action: DataOperationAction, request: DataOperationsWriteRequest): string {
    return createHash('sha256')
      .update(`${action}:${canonicalJson(request)}`)
      .digest('hex');
  }

  /** 生成不含账号、邮箱或 JWT 的稳定 opaque actorRef。 */
  private actorRef(actorId: string): string {
    return `user:${createHmac('sha256', this.config.jwtAccessSecret).update(actorId).digest('base64url')}`;
  }

  /** 构造唯一允许写入 data-sync 的内部 body，并把理由仅放在 actor 上下文。 */
  private internalPayload(
    action: DataOperationAction,
    submissionId: string,
    actorRef: string,
    role: Role,
    request: DataOperationsWriteRequest,
  ): Record<string, unknown> {
    const actor = { actorRef, role, reason: request.reason };
    const source = asRecord(request);
    if (!source) {
      throw new Error('validated data operations request is not an object');
    }
    if (action === 'SYNC_SUBMIT') {
      return {
        submissionId,
        preflightId: source.preflightId,
        requestHash: source.requestHash,
        targets: source.targets,
        actor,
      };
    }
    if (action === 'SYNC_CANCEL' || action === 'SYNC_RETRY') {
      return { submissionId, target: source.target, actor };
    }
    if (action === 'HEALTH_CHECK_SUBMIT') {
      return { submissionId, targets: source.targets, actor };
    }
    if (action === 'SCHEDULE_UPSERT') {
      return {
        submissionId,
        scheduleId: source.scheduleId,
        datasetCode: source.datasetCode,
        mode: source.mode,
        selector: source.selector,
        targetPolicy: source.targetPolicy,
        frequency: source.frequency,
        misfirePolicy: source.misfirePolicy,
        coalesce: source.coalesce,
        enabled: source.enabled,
        expectedVersion: source.expectedVersion,
        actor,
      };
    }
    return {
      submissionId,
      scheduleId: source.scheduleId,
      enabled: source.enabled,
      expectedVersion: source.expectedVersion,
      actor,
    };
  }

  /** 限制冻结 outbox JSON 的 UTF-8 大小，防止单次提交耗尽数据库与内部 HTTP 边界。 */
  private assertOutboxPayloadSize(payload: Record<string, unknown>): void {
    const size = Buffer.byteLength(canonicalJson(payload), 'utf8');
    if (size > MAX_OUTBOX_PAYLOAD_BYTES) {
      throw new PublicProblemException(
        HttpStatus.UNPROCESSABLE_ENTITY,
        'outbox-payload-too-large',
        'Data operations request exceeds the delivery payload limit',
      );
    }
  }

  /** 生成审计与操作记录可显示的有界目标摘要，不保留原始请求或 Provider 参数。 */
  private targetSummary(action: DataOperationAction, request: DataOperationsWriteRequest): string {
    const source = asRecord(request);
    if (!source) {
      return action;
    }
    if ('targets' in source && Array.isArray(source.targets)) {
      const codes = source.targets
        .map((target) => asRecord(target)?.datasetCode)
        .filter((value): value is string => typeof value === 'string')
        .slice(0, 100);
      return `${action}: ${codes.join(', ')}`.slice(0, 300);
    }
    if ('target' in source) {
      const target = asRecord(source.target);
      const resourceType =
        typeof target?.resourceType === 'string' ? target.resourceType : 'UNKNOWN';
      const resourceId = typeof target?.resourceId === 'string' ? target.resourceId : '';
      return `${action}: ${resourceType} ${resourceId}`.slice(0, 300);
    }
    const datasetCode =
      typeof source.datasetCode === 'string'
        ? source.datasetCode
        : typeof source.scheduleId === 'string'
          ? source.scheduleId
          : '';
    return `${action}: ${datasetCode}`.slice(0, 300);
  }

  /** 从 JSONB 仅恢复符合公开 ErrorSummary 形状的脱敏错误。 */
  private safeError(value: Prisma.JsonValue | null): SafeError | null {
    const record = asRecord(value);
    if (!record) {
      return null;
    }
    const code = typeof record.code === 'string' ? record.code : null;
    const stage = typeof record.stage === 'string' ? record.stage : null;
    const retryable = typeof record.retryable === 'boolean' ? record.retryable : null;
    const message = typeof record.message === 'string' ? record.message : null;
    if (
      code === null ||
      stage === null ||
      retryable === null ||
      message === null ||
      message.length > 500
    ) {
      return null;
    }
    return { code: code.slice(0, 80), stage: stage as SafeError['stage'], retryable, message };
  }

  /** 防止服务层或未来任务调用绕过 SUPER_ADMIN 写权限。 */
  private assertWriter(actor: AuthContext): void {
    if (actor.role !== Role.SUPER_ADMIN) {
      throw new PublicProblemException(
        HttpStatus.FORBIDDEN,
        'forbidden',
        'Data operations write requires super-administrator authority',
      );
    }
  }
}

const MAX_OUTBOX_PAYLOAD_BYTES = 64 * 1024;

/** 将已通过 Zod 校验的冻结请求递归转换为 Prisma 可持久化 JSON 对象。 */
function prismaJsonObject(value: Record<string, unknown>): Prisma.InputJsonObject {
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, prismaJsonValue(item)]),
  );
}

/** 递归限制 JSON 值类型，避免把函数、日期对象或任意原型对象写入 outbox。 */
function prismaJsonValue(value: unknown): Prisma.InputJsonValue | null {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => prismaJsonValue(item));
  }
  const record = asRecord(value);
  if (record) {
    return prismaJsonObject(record);
  }
  throw new Error('validated data operations request contains non-JSON value');
}

/** 构建本地 Submission 的闭合授权时间窗，避免 gte/lte 条件互相覆盖。 */
function dateWindow(
  occurredFrom: string | null | undefined,
  occurredTo: string | null | undefined,
): Pick<Prisma.DataOperationSubmissionWhereInput, 'authorizedAt'> | Record<string, never> {
  const authorizedAt = {
    ...(occurredFrom === undefined || occurredFrom === null ? {} : { gte: new Date(occurredFrom) }),
    ...(occurredTo === undefined || occurredTo === null ? {} : { lte: new Date(occurredTo) }),
  };
  return Object.keys(authorizedAt).length === 0 ? {} : { authorizedAt };
}
