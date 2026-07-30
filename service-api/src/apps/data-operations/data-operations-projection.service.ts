import { HttpStatus, Injectable } from '@nestjs/common';
import { createHash } from 'node:crypto';

import type { DataOperationSubmission, Prisma } from '../../generated/prisma/client.js';
import type { AuthContext } from '../../common/models/auth-context.js';
import { DataOperationsClient } from '../../data-sync/clients/data-operations.client.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import {
  asRecord,
  canonicalJson,
  iso,
  nullableString,
  requiredArray,
  requiredString,
  type ActorDisplay,
  type AuthorityResource,
  type SafeError,
} from './data-operations.types.js';
import { DataOperationSubmissionService } from './data-operation-submission.service.js';

/** 将 data-sync 内部主体和事件投影为 Web 可安全显示的操作者与操作记录。 */
@Injectable()
export class DataOperationsProjectionService {
  /** 注入本地身份投影、Submission 账本和 data-sync 只读客户端。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly submissions: DataOperationSubmissionService,
    private readonly client: DataOperationsClient,
  ) {}

  /** 将 opaque actorRef 与可选 Submission 关联成不泄露内部引用的 ActorDisplay。 */
  public async actorDisplay(
    actorRef: string | null,
    submissionId: string | null,
  ): Promise<ActorDisplay> {
    const system = systemActor(actorRef);
    if (system) {
      return system;
    }
    let submission:
      | (DataOperationSubmission & {
          actor: { id: string; displayName: string; status: string } | null;
        })
      | null;
    try {
      submission =
        submissionId === null
          ? actorRef === null
            ? null
            : await this.database.client.dataOperationSubmission.findFirst({
                where: { actorRef },
                include: { actor: { select: { id: true, displayName: true, status: true } } },
              })
          : await this.database.client.dataOperationSubmission.findUnique({
              where: { id: submissionId },
              include: { actor: { select: { id: true, displayName: true, status: true } } },
            });
    } catch {
      throw unavailable();
    }
    if (!submission?.actor || submission.actor.status === 'DELETED') {
      return {
        actorType: 'USER',
        systemKind: null,
        actorId: null,
        displayName: '已删除用户',
        deleted: true,
      };
    }
    return {
      actorType: 'USER',
      systemKind: null,
      actorId: submission.actor.id,
      displayName: submission.actor.displayName,
      deleted: false,
    };
  }

  /** 搜索用户 Submission 与系统 data-sync 事件，形成窄化的操作记录页。 */
  public async searchOperations(
    actor: AuthContext,
    input: {
      cursor?: string | null | undefined;
      limit?: number | undefined;
      actorIds?: string[] | undefined;
      actions?: string[] | undefined;
      deliveryStatuses?: string[] | undefined;
      operationResults?: string[] | undefined;
      occurredFrom?: string | null | undefined;
      occurredTo?: string | null | undefined;
    },
    requestId: string,
  ): Promise<{ items: unknown[]; nextCursor: string | null }> {
    try {
      return await this.searchOperationPage(actor.userId, input, requestId);
    } catch (error: unknown) {
      if (error instanceof PublicProblemException) {
        throw error;
      }
      this.client.asPublicProblem(error);
    }
  }

  /** 以数据库保存的双流游标合并本地 Submission 和 data-sync 事件，不丢页也不重复页。 */
  private async searchOperationPage(
    actorId: string,
    input: OperationSearchInput,
    requestId: string,
  ): Promise<{ items: unknown[]; nextCursor: string | null }> {
    const state = await this.loadOperationCursor(actorId, input);
    const limit = input.limit ?? 50;
    const items: Record<string, unknown>[] = [];
    const relatedRequestIds = new Map<string, boolean>();
    let local: LocalCandidate | null | undefined;
    let event: EventCandidate | null | undefined;

    while (items.length < limit && (!state.localExhausted || !state.eventExhausted)) {
      if (local === undefined && !state.localExhausted) {
        const page = await this.submissions.searchSubmissions({
          ...input,
          occurredTo: iso(state.occurredTo),
          cursor:
            state.localAuthorizedAt === null || state.localId === null
              ? null
              : { authorizedAt: state.localAuthorizedAt, id: state.localId },
          // 额外读取一项仅用于准确判断本地流是否耗尽，不会提前推进游标。
          limit: 2,
        });
        const first = page.at(0);
        local = first === undefined ? null : { submission: first, exhaustedAfter: page.length < 2 };
        if (local === null) {
          state.localExhausted = true;
        }
      }
      if (event === undefined && !state.eventExhausted) {
        event = await this.readEventCandidate(state, input, requestId);
      }

      // 由 service-api 发起的 data-sync 事件由本地 Submission 统一展示，避免同一操作出现两行。
      while (event !== null && event !== undefined) {
        const eventRequestId = requiredString(event.record, 'requestId');
        if (!eventRequestId) {
          throw unavailable();
        }
        const related = await this.isRelatedRequest(eventRequestId, relatedRequestIds);
        if (related || !eventMatchesFilters(event.record, input)) {
          this.advanceEventCursor(state, event);
          event = state.eventExhausted
            ? null
            : await this.readEventCandidate(state, input, requestId);
          continue;
        }
        break;
      }

      if (local === null && (event === null || event === undefined)) {
        break;
      }
      if (local !== null && local !== undefined && shouldPreferLocal(local, event)) {
        items.push(await this.submissionRecord(local.submission));
        state.localAuthorizedAt = local.submission.authorizedAt;
        state.localId = local.submission.id;
        state.localExhausted = local.exhaustedAfter;
        local = undefined;
        continue;
      }
      if (event !== null && event !== undefined) {
        items.push(await this.systemEventRecord(event.record));
        this.advanceEventCursor(state, event);
        event = undefined;
        continue;
      }
    }

    if (items.length === 0 && state.localExhausted && state.eventExhausted) {
      return { items, nextCursor: null };
    }
    if (state.localExhausted && state.eventExhausted) {
      return { items, nextCursor: null };
    }
    return { items, nextCursor: await this.persistOperationCursor(state) };
  }

  /** 读取一条系统事件候选项，并把空页或错误分页状态转换为安全依赖故障。 */
  private async readEventCandidate(
    state: OperationCursorState,
    input: OperationSearchInput,
    requestId: string,
  ): Promise<EventCandidate | null> {
    const page = await this.client.searchEvents(
      {
        cursor: state.eventCursor,
        limit: 1,
        ...(input.actions === undefined ? {} : { actions: input.actions }),
        ...(input.occurredFrom === undefined ? {} : { occurredFrom: input.occurredFrom }),
        occurredTo: iso(state.occurredTo),
      },
      requestId,
    );
    const events = requiredArray(page, 'items');
    const nextCursor = nullableString(page, 'nextCursor');
    if (!events || nextCursor === undefined || events.length > 1) {
      throw unavailable();
    }
    if (events.length === 0) {
      if (nextCursor === null) {
        state.eventExhausted = true;
        return null;
      }
      // 下游可因已裁剪的事件返回空页；仍须推进 opaque cursor 才不会陷入死循环。
      state.eventCursor = nextCursor;
      return this.readEventCandidate(state, input, requestId);
    }
    const record = asRecord(events[0]);
    if (!record) {
      throw unavailable();
    }
    return { record, nextCursor };
  }

  /** 判断不可变事件是否对应一个本地 Submission，结果在单页内缓存以减少数据库读取。 */
  private async isRelatedRequest(requestId: string, cache: Map<string, boolean>): Promise<boolean> {
    const cached = cache.get(requestId);
    if (cached !== undefined) {
      return cached;
    }
    const related = await this.submissions.findByRequestIds([requestId]);
    const matched = related.length > 0;
    cache.set(requestId, matched);
    return matched;
  }

  /** 将已消费或已去重的系统事件推进至其下游 opaque cursor。 */
  private advanceEventCursor(state: OperationCursorState, event: EventCandidate): void {
    state.eventCursor = event.nextCursor;
    state.eventExhausted = event.nextCursor === null;
  }

  /** 载入或初始化调用方专属的联合游标，并冻结首次检索的 occurredTo 上界。 */
  private async loadOperationCursor(
    actorId: string,
    input: OperationSearchInput,
  ): Promise<OperationCursorState> {
    const fingerprint = operationFingerprint(input);
    if (!input.cursor) {
      const occurredTo =
        input.occurredTo === null || input.occurredTo === undefined
          ? new Date()
          : new Date(input.occurredTo);
      if (Number.isNaN(occurredTo.getTime())) {
        throw invalidCursor();
      }
      return {
        id: null,
        actorId,
        fingerprint,
        occurredTo,
        localAuthorizedAt: null,
        localId: null,
        eventCursor: null,
        localExhausted: false,
        eventExhausted: false,
        version: null,
      };
    }
    if (!isUuid(input.cursor)) {
      throw invalidCursor();
    }
    const cursor = await this.database.client.dataOperationSearchCursor.findUnique({
      where: { id: input.cursor },
    });
    if (!cursor || cursor.actorId !== actorId || cursor.fingerprint !== fingerprint) {
      throw invalidCursor();
    }
    return {
      id: cursor.id,
      actorId: cursor.actorId,
      fingerprint: cursor.fingerprint,
      occurredTo: cursor.occurredTo,
      localAuthorizedAt: cursor.localAuthorizedAt,
      localId: cursor.localId,
      eventCursor: cursor.eventCursor,
      localExhausted: cursor.localExhausted,
      eventExhausted: cursor.eventExhausted,
      version: cursor.version,
    };
  }

  /** 创建或乐观更新联合游标；重复并发消费同一 cursor 会被拒绝而不是重复发页。 */
  private async persistOperationCursor(state: OperationCursorState): Promise<string> {
    if (state.id === null || state.version === null) {
      const created = await this.database.client.dataOperationSearchCursor.create({
        data: {
          actorId: state.actorId,
          fingerprint: state.fingerprint,
          occurredTo: state.occurredTo,
          localAuthorizedAt: state.localAuthorizedAt,
          localId: state.localId,
          eventCursor: state.eventCursor,
          localExhausted: state.localExhausted,
          eventExhausted: state.eventExhausted,
        },
      });
      return created.id;
    }
    const updated = await this.database.client.dataOperationSearchCursor.updateMany({
      where: {
        id: state.id,
        actorId: state.actorId,
        fingerprint: state.fingerprint,
        version: state.version,
      },
      data: {
        localAuthorizedAt: state.localAuthorizedAt,
        localId: state.localId,
        eventCursor: state.eventCursor,
        localExhausted: state.localExhausted,
        eventExhausted: state.eventExhausted,
        version: { increment: 1 },
      },
    });
    if (updated.count !== 1) {
      throw new PublicProblemException(
        HttpStatus.CONFLICT,
        'cursor-already-consumed',
        'Data operations cursor was consumed concurrently',
      );
    }
    return state.id;
  }

  /** 以可公开的 actor、理由和状态机字段投影一条 API Submission。 */
  public async submissionRecord(
    submission: DataOperationSubmission & {
      actor?: { id: string; displayName: string; status: string } | null;
    },
  ): Promise<Record<string, unknown>> {
    const authorityResource = authority(submission.authorityType, submission.authorityId);
    const actor =
      submission.actor && submission.actor.status !== 'DELETED'
        ? {
            actorType: 'USER',
            systemKind: null,
            actorId: submission.actor.id,
            displayName: submission.actor.displayName,
            deleted: false,
          }
        : await this.actorDisplay(submission.actorRef, submission.id);
    return {
      submissionId: submission.id,
      action: submission.action,
      targetSummary: targetSummary(submission.action, submission.sanitizedRequest),
      actor,
      reason: submission.reason,
      deliveryStatus: submission.deliveryStatus,
      operationResult: submission.operationResult,
      authorityResource,
      authorizedAt: iso(submission.authorizedAt),
      completedAt: submission.completedAt === null ? null : iso(submission.completedAt),
      lastObservedAt: submission.lastObservedAt === null ? null : iso(submission.lastObservedAt),
      requestId: submission.requestId,
      error: safeError(submission.safeError),
    };
  }

  /** 将没有本地 Submission 的 data-sync 事件投影为明确的 SYSTEM 操作记录。 */
  private async systemEventRecord(
    event: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const resourceType = requiredString(event, 'resourceType');
    const resourceId = requiredString(event, 'resourceId');
    const action = requiredString(event, 'action');
    const result = requiredString(event, 'result');
    const requestId = requiredString(event, 'requestId');
    const occurredAt = requiredString(event, 'occurredAt');
    const actorRef = requiredString(event, 'actorRef');
    if (
      !resourceType ||
      !resourceId ||
      !action ||
      !result ||
      !requestId ||
      !occurredAt ||
      !actorRef
    ) {
      throw unavailable();
    }
    const operationResult = eventOperationResult(result);
    return {
      submissionId: null,
      action,
      targetSummary: `${resourceType}: ${resourceId}`.slice(0, 300),
      actor: await this.actorDisplay(actorRef, null),
      reason: '系统触发',
      deliveryStatus: 'NOT_APPLICABLE',
      operationResult,
      authorityResource: { resourceType, resourceId },
      authorizedAt: occurredAt,
      completedAt: isTerminalResult(operationResult) ? occurredAt : null,
      lastObservedAt: occurredAt,
      requestId,
      error: safeError(event.error),
    };
  }
}

/** 表示公开操作记录检索的受限过滤器；该类型不包含数据库或 data-sync 内部字段。 */
type OperationSearchInput = {
  cursor?: string | null | undefined;
  limit?: number | undefined;
  actorIds?: string[] | undefined;
  actions?: string[] | undefined;
  deliveryStatuses?: string[] | undefined;
  operationResults?: string[] | undefined;
  occurredFrom?: string | null | undefined;
  occurredTo?: string | null | undefined;
};

/** 表示保存在 API 数据库中的双流联合游标运行态。 */
type OperationCursorState = {
  id: string | null;
  actorId: string;
  fingerprint: string;
  occurredTo: Date;
  localAuthorizedAt: Date | null;
  localId: string | null;
  eventCursor: string | null;
  localExhausted: boolean;
  eventExhausted: boolean;
  version: number | null;
};

/** 表示尚未消费的一条本地 Submission 及其后继页是否耗尽。 */
type LocalCandidate = {
  submission: DataOperationSubmission & {
    actor: { id: string; displayName: string; status: string } | null;
  };
  exhaustedAfter: boolean;
};

/** 表示尚未消费的一条 data-sync 事件和推进它所需的 opaque cursor。 */
type EventCandidate = {
  record: Record<string, unknown>;
  nextCursor: string | null;
};

/** 生成与调用方、筛选器绑定的稳定查询指纹，防止 cursor 被改筛选条件后重放。 */
function operationFingerprint(input: OperationSearchInput): string {
  const normalized = {
    actorIds: normalizedArray(input.actorIds),
    actions: normalizedArray(input.actions),
    deliveryStatuses: normalizedArray(input.deliveryStatuses),
    operationResults: normalizedArray(input.operationResults),
    occurredFrom: input.occurredFrom ?? null,
    occurredTo: input.occurredTo ?? null,
  };
  return createHash('sha256').update(canonicalJson(normalized)).digest('hex');
}

/** 将等价的筛选数组规范化，避免仅因顺序变化使续页 cursor 失效。 */
function normalizedArray(values: string[] | undefined): string[] | null {
  return values === undefined ? null : [...new Set(values)].sort();
}

/** 判断本地 Submission 是否应排在当前系统事件之前，时间相等时固定优先本地流。 */
function shouldPreferLocal(
  local: LocalCandidate,
  event: EventCandidate | null | undefined,
): boolean {
  if (!event) {
    return true;
  }
  const occurredAt = requiredString(event.record, 'occurredAt');
  if (!occurredAt) {
    throw unavailable();
  }
  const eventTime = new Date(occurredAt).getTime();
  if (Number.isNaN(eventTime)) {
    throw unavailable();
  }
  return local.submission.authorizedAt.getTime() >= eventTime;
}

/** 根据公开筛选器判断 data-sync 事件是否应以 SYSTEM 记录展示。 */
function eventMatchesFilters(event: Record<string, unknown>, input: OperationSearchInput): boolean {
  // actorIds 只包含公开 User UUID；系统事件没有可安全反查的用户 UUID，必须排除。
  if (input.actorIds !== undefined) {
    return false;
  }
  if (input.deliveryStatuses !== undefined && !input.deliveryStatuses.includes('NOT_APPLICABLE')) {
    return false;
  }
  const action = requiredString(event, 'action');
  const result = requiredString(event, 'result');
  if (!action || !result) {
    throw unavailable();
  }
  if (input.actions !== undefined && !input.actions.includes(action)) {
    return false;
  }
  const operationResult = eventOperationResult(result);
  return input.operationResults === undefined || input.operationResults.includes(operationResult);
}

/** 将 data-sync 事件的 ACCEPTED/STARTED 动作结果转换为公开 OperationResult 枚举。 */
function eventOperationResult(result: string): string {
  if (result === 'ACCEPTED') {
    return 'QUEUED';
  }
  if (result === 'STARTED') {
    return 'RUNNING';
  }
  if (
    [
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
    ].includes(result)
  ) {
    return result;
  }
  throw unavailable();
}

/** 校验 API 自管 cursor 的 UUID 外形，避免数据库类型错误泄露为 5xx。 */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

/** 返回不暴露游标存储细节的稳定公开错误。 */
function invalidCursor(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.BAD_REQUEST,
    'invalid-cursor',
    'Data operations cursor is invalid',
  );
}

/** 将系统 actorRef 映射为固定中文显示名，避免误标为已删除用户。 */
function systemActor(actorRef: string | null): ActorDisplay | null {
  if (actorRef?.startsWith('system:schedule')) {
    return {
      actorType: 'SYSTEM',
      systemKind: 'SCHEDULE',
      actorId: null,
      displayName: '系统计划',
      deleted: false,
    };
  }
  if (actorRef?.startsWith('system:legacy')) {
    return {
      actorType: 'SYSTEM',
      systemKind: 'LEGACY',
      actorId: null,
      displayName: '遗留任务',
      deleted: false,
    };
  }
  if (actorRef?.startsWith('system:recovery')) {
    return {
      actorType: 'SYSTEM',
      systemKind: 'RECOVERY',
      actorId: null,
      displayName: '系统恢复',
      deleted: false,
    };
  }
  if (actorRef?.startsWith('system:')) {
    return {
      actorType: 'SYSTEM',
      systemKind: 'OTHER',
      actorId: null,
      displayName: '系统任务',
      deleted: false,
    };
  }
  return null;
}

/** 将数据库权威资源列安全映射为公开对象，半空组合不会泄漏为无效跳转。 */
function authority(type: string | null, id: string | null): AuthorityResource | null {
  if (
    id === null ||
    type === null ||
    !['COMMAND', 'RUN', 'HEALTH_CHECK', 'SCHEDULE'].includes(type)
  ) {
    return null;
  }
  return { resourceType: type as AuthorityResource['resourceType'], resourceId: id };
}

/** 从已脱敏 JSONB request 重建有界目标摘要，不返回请求原文。 */
function targetSummary(action: string, value: Prisma.JsonValue): string {
  const record = asRecord(value);
  if (!record) {
    return action;
  }
  if (Array.isArray(record.targets)) {
    const codes = record.targets
      .map((target) => asRecord(target)?.datasetCode)
      .filter((item): item is string => typeof item === 'string')
      .slice(0, 100);
    return `${action}: ${codes.join(', ')}`.slice(0, 300);
  }
  const target = asRecord(record.target);
  if (target) {
    const resourceType = typeof target.resourceType === 'string' ? target.resourceType : 'UNKNOWN';
    const resourceId = typeof target.resourceId === 'string' ? target.resourceId : '';
    return `${action}: ${resourceType} ${resourceId}`.slice(0, 300);
  }
  const datasetCode =
    typeof record.datasetCode === 'string'
      ? record.datasetCode
      : typeof record.scheduleId === 'string'
        ? record.scheduleId
        : '';
  return `${action}: ${datasetCode}`.slice(0, 300);
}

/** 仅投影完整 ErrorSummary 白名单，避免 JSONB 中的意外字段出现在 Web。 */
function safeError(value: unknown): SafeError | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }
  const code = typeof record.code === 'string' ? record.code : null;
  const stage = typeof record.stage === 'string' ? record.stage : null;
  const retryable = typeof record.retryable === 'boolean' ? record.retryable : null;
  const message = typeof record.message === 'string' ? record.message : null;
  if (!code || !stage || retryable === null || !message || message.length > 500) {
    return null;
  }
  return { code: code.slice(0, 80), stage: stage as SafeError['stage'], retryable, message };
}

/** 判断操作结果是否已经不会再推进，用于系统事件的 completedAt 投影。 */
function isTerminalResult(result: string): boolean {
  // INTERRUPTED 是可恢复中间态，系统事件投影必须保留 completedAt=null 等待最终事件。
  return ['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED', 'SKIPPED', 'REJECTED'].includes(result);
}

/** 返回不包含下游正文的统一服务不可用错误。 */
function unavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Data operations are temporarily unavailable',
  );
}
