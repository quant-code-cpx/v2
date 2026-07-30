import { HttpStatus, Injectable } from '@nestjs/common';

import { Role } from '../../generated/prisma/client.js';
import type { AuthContext } from '../../common/models/auth-context.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { DataOperationsClient } from '../../data-sync/clients/data-operations.client.js';
import {
  draftTargetSelectorSchema,
  scheduleFrequencySchema,
  scheduleTargetPolicySchema,
  syncFrozenTargetSchema,
  syncTargetSchema,
  targetSelectorMatchesDataset,
} from '../../data-sync/contracts/data-operations.contract.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import {
  asRecord,
  nullableString,
  requiredArray,
  requiredString,
  type SafeError,
} from './data-operations.types.js';
import { DataOperationsProjectionService } from './data-operations-projection.service.js';

/** 提供 data-sync 权威读取的公开安全投影，不缓存或推导同步事实。 */
@Injectable()
export class DataOperationsQueryService {
  /** 注入内部 HTTP client、本地交付计数与身份投影能力。 */
  public constructor(
    private readonly client: DataOperationsClient,
    private readonly database: DatabaseService,
    private readonly projection: DataOperationsProjectionService,
  ) {}

  /** 查询 data-sync 总览并附加 API 本地待交付与死信数。 */
  public async overview(actor: AuthContext, requestId: string): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    try {
      const [dataSync, pending, deadLetter] = await Promise.all([
        this.client.overview(requestId),
        this.database.client.dataOperationSubmission.count({
          where: { deliveryStatus: { in: ['PENDING', 'DELIVERING'] } },
        }),
        this.database.client.dataOperationSubmission.count({
          where: { deliveryStatus: 'DEAD_LETTER' },
        }),
      ]);
      return {
        dataSync: this.overviewProjection(dataSync),
        deliveryPendingCount: pending,
        deliveryDeadLetterCount: deadLetter,
      };
    } catch (error: unknown) {
      this.rethrowReadError(error);
    }
  }

  /** 查询数据资产页，删除只属于 data-sync 的 schemaVersion。 */
  public async searchDatasets(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const page = await this.read(() => this.client.searchDatasets(input, requestId));
    const items = boundedArray(page, 'items', 100);
    const nextCursor = nullableString(page, 'nextCursor');
    const totalEstimate = page.totalEstimate;
    const generatedAt = requiredString(page, 'generatedAt');
    if (
      !items ||
      nextCursor === undefined ||
      !Number.isSafeInteger(totalEstimate) ||
      !generatedAt
    ) {
      throw unavailable();
    }
    return {
      items: items.map((item) => this.datasetSummary(item)),
      nextCursor,
      totalEstimate,
      generatedAt,
    };
  }

  /** 查询单个数据资产详情，并保持 MODEL_ONLY 的 freshness null 语义。 */
  public async getDataset(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const detail = await this.read(() => this.client.getDataset(input, requestId));
    const summary = this.datasetSummary(detail.summary);
    const description = requiredString(detail, 'description');
    const grain = requiredString(detail, 'grain');
    const healthRules = boundedArray(detail, 'healthRules', 200);
    if (!description || !grain || !healthRules || !('freshnessPolicy' in detail)) {
      throw unavailable();
    }
    return {
      summary,
      description,
      grain,
      freshnessPolicy:
        detail.freshnessPolicy === null
          ? null
          : pickObject(detail.freshnessPolicy, [
              'timezone',
              'calendarCode',
              'warnAfterMinutes',
              'criticalAfterMinutes',
            ]),
      latestPublication:
        detail.latestPublication === null
          ? null
          : pickObject(detail.latestPublication, [
              'dataVersion',
              'releaseId',
              'publishedAt',
              'rowCount',
            ]),
      latestError: safeError(detail.latestError),
      healthRules: healthRules.map((rule) =>
        pickObject(rule, ['ruleCode', 'dimension', 'severity', 'version']),
      ),
    };
  }

  /** 将无副作用 preflight 原样经严格内部边界返回给超级管理员。 */
  public async preflight(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertWriter(actor);
    const preflight = await this.read(() => this.client.preflight(input, requestId));
    const targets = boundedArray(preflight, 'targets', 100);
    if (
      !targets ||
      !requiredString(preflight, 'preflightId') ||
      !requiredString(preflight, 'requestHash')
    ) {
      throw unavailable();
    }
    return {
      preflightId: preflight.preflightId,
      requestHash: preflight.requestHash,
      expiresAt: preflight.expiresAt,
      queueDepth: preflight.queueDepth,
      executionSlot: this.overviewSlot(preflight.executionSlot),
      targets: targets.map(preflightTarget),
      accepted: preflight.accepted,
    };
  }

  /** 查询命令详情并将内部 actorRef 映射为 ActorDisplay。 */
  public async getCommand(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const detail = await this.read(() => this.client.getCommand(input, requestId));
    const submissionId = nullableString(detail, 'submissionId');
    const actorRef = requiredString(detail, 'actorRef');
    const childRuns = boundedArray(detail, 'childRuns', 100);
    if (submissionId === undefined || !actorRef || !childRuns) {
      throw unavailable();
    }
    return {
      commandId: requiredField(detail, 'commandId'),
      submissionId,
      status: requiredField(detail, 'status'),
      requestedAt: requiredField(detail, 'requestedAt'),
      startedAt: nullableField(detail, 'startedAt'),
      finishedAt: nullableField(detail, 'finishedAt'),
      childRuns: childRuns.map((run) => this.runSummary(run)),
      requestedBy: await this.projection.actorDisplay(actorRef, submissionId),
      error: safeError(detail.error),
    };
  }

  /** 查询运行搜索页，运行摘要不包含内部 checkpoint 或 fencing token。 */
  public async searchRuns(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const page = await this.read(() => this.client.searchRuns(input, requestId));
    const items = boundedArray(page, 'items', 100);
    const nextCursor = nullableString(page, 'nextCursor');
    if (!items || nextCursor === undefined) {
      throw unavailable();
    }
    return { items: items.map((item) => this.runSummary(item)), nextCursor };
  }

  /** 查询运行详情并删除 actorRef、fencingToken 和每个 partition 的 checkpoint。 */
  public async getRun(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const detail = await this.read(() => this.client.getRun(input, requestId));
    const run = this.runSummary(detail.run);
    const actorRef = requiredString(detail, 'actorRef');
    const sourceSnapshot = boundedArray(detail, 'sourceSnapshot', 10);
    const partitions = boundedArray(detail, 'partitions', 100);
    const timeline = boundedArray(detail, 'timeline', 100);
    if (!actorRef || !sourceSnapshot || !partitions || !timeline) {
      throw unavailable();
    }
    return {
      run,
      target: syncTarget(detail.target, true),
      sourceSnapshot: sourceSnapshot.map(sourceBinding),
      qualityGate: qualityGate(detail.qualityGate),
      partitionCount: requiredField(detail, 'partitionCount'),
      partitions: partitions.map((partition) => {
        const value = asRecord(partition);
        if (!value) throw unavailable();
        return {
          partitionKey: requiredField(value, 'partitionKey'),
          status: requiredField(value, 'status'),
          attempt: requiredField(value, 'attempt'),
          error: safeError(value.error),
        };
      }),
      partitionsNextCursor: nullableField(detail, 'partitionsNextCursor'),
      timelineEventCount: requiredField(detail, 'timelineEventCount'),
      timeline: await Promise.all(timeline.map((event) => this.operationEvent(event))),
      timelineNextCursor: nullableField(detail, 'timelineNextCursor'),
      requestedBy: await this.projection.actorDisplay(actorRef, null),
    };
  }

  /** 查询健康评估摘要页，不在列表泄漏逐规则结果。 */
  public async searchHealthEvaluations(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const page = await this.read(() => this.client.searchHealthEvaluations(input, requestId));
    const items = boundedArray(page, 'items', 100);
    const nextCursor = nullableString(page, 'nextCursor');
    if (!items || nextCursor === undefined) {
      throw unavailable();
    }
    return {
      items: items.map((item) =>
        pickObject(item, [
          'evaluationId',
          'healthCheckId',
          'datasetCode',
          'dataVersion',
          'releaseId',
          'policyCode',
          'policyVersion',
          'status',
          'score',
          'evaluatedAt',
          'warningCount',
          'criticalCount',
          'currentOpenIssueCount',
          'issueProjectionAsOf',
          'affectedRecordCount',
        ]),
      ),
      nextCursor,
    };
  }

  /** 查询不可变健康评估和当前开放问题投影。 */
  public async getHealthEvaluation(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const detail = await this.read(() => this.client.getHealthEvaluation(input, requestId));
    const evaluation = asRecord(detail.evaluation);
    const issues = boundedArray(detail, 'currentOpenIssues', 100);
    if (!evaluation || !issues) {
      throw unavailable();
    }
    const results = boundedArray(evaluation, 'results', 200);
    if (!results) {
      throw unavailable();
    }
    return {
      evaluation: {
        ...pickRecord(evaluation, [
          'evaluationId',
          'healthCheckId',
          'datasetCode',
          'dataVersion',
          'releaseId',
          'policyCode',
          'policyVersion',
          'status',
          'score',
          'evaluatedAt',
        ]),
        results: results.map((result) =>
          pickObject(result, [
            'ruleCode',
            'dimension',
            'severity',
            'status',
            'expected',
            'observed',
            'affectedCount',
            'sampleSummary',
            'message',
          ]),
        ),
      },
      currentOpenIssueCount: requiredField(detail, 'currentOpenIssueCount'),
      currentOpenIssues: issues.map((issue) =>
        pickObject(issue, [
          'issueId',
          'ruleCode',
          'dimension',
          'severity',
          'status',
          'firstDetectedAt',
          'lastDetectedAt',
          'affectedCount',
          'evidenceSummary',
        ]),
      ),
      currentOpenIssuesNextCursor: nullableField(detail, 'currentOpenIssuesNextCursor'),
      issueProjectionAsOf: requiredField(detail, 'issueProjectionAsOf'),
    };
  }

  /** 查询批量健康检查详情并投影请求主体。 */
  public async getHealthCheck(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const detail = await this.read(() => this.client.getHealthCheck(input, requestId));
    const submissionId = nullableString(detail, 'submissionId');
    const actorRef = requiredString(detail, 'actorRef');
    const targets = boundedArray(detail, 'targets', 100);
    if (submissionId === undefined || !actorRef || !targets) {
      throw unavailable();
    }
    return {
      healthCheckId: requiredField(detail, 'healthCheckId'),
      submissionId,
      status: requiredField(detail, 'status'),
      requestedAt: requiredField(detail, 'requestedAt'),
      startedAt: nullableField(detail, 'startedAt'),
      finishedAt: nullableField(detail, 'finishedAt'),
      requestedBy: await this.projection.actorDisplay(actorRef, submissionId),
      targets: targets.map((target) => {
        const value = asRecord(target);
        if (!value) throw unavailable();
        return {
          target: pickObject(value.target, ['datasetCode', 'dataVersion']),
          resolvedDataVersion: nullableField(value, 'resolvedDataVersion'),
          status: requiredField(value, 'status'),
          evaluationId: nullableField(value, 'evaluationId'),
          error: safeError(value.error),
        };
      }),
      error: safeError(detail.error),
    };
  }

  /** 查询自动计划页，并将内部 updatedByActorRef 投影为 ActorDisplay。 */
  public async searchSchedules(
    actor: AuthContext,
    input: unknown,
    requestId: string,
  ): Promise<Record<string, unknown>> {
    this.assertReader(actor);
    const page = await this.read(() => this.client.searchSchedules(input, requestId));
    const items = boundedArray(page, 'items', 100);
    const nextCursor = nullableString(page, 'nextCursor');
    if (!items || nextCursor === undefined) {
      throw unavailable();
    }
    return { items: await Promise.all(items.map((item) => this.scheduleView(item))), nextCursor };
  }

  /** 查询允许 ADMIN 与 SUPER_ADMIN 查看的窄化操作记录。 */
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
    this.assertReader(actor);
    return this.projection.searchOperations(actor, input, requestId);
  }

  /** 通过 data-sync 读取 client 执行函数，并统一将依赖故障映射为安全公开错误。 */
  private async read(
    operation: () => Promise<Record<string, unknown>>,
  ): Promise<Record<string, unknown>> {
    try {
      return await operation();
    } catch (error: unknown) {
      this.rethrowReadError(error);
    }
  }

  /** 将 data-sync 读取失败保持为合同状态，数据库失败则不返回部分伪事实。 */
  private rethrowReadError(error: unknown): never {
    if (error instanceof PublicProblemException) {
      throw error;
    }
    this.client.asPublicProblem(error);
  }

  /** 保证服务层调用也遵循 ADMIN/SUPER_ADMIN 读取边界。 */
  private assertReader(actor: AuthContext): void {
    if (actor.role !== Role.ADMIN && actor.role !== Role.SUPER_ADMIN) {
      throw new PublicProblemException(
        HttpStatus.FORBIDDEN,
        'forbidden',
        'Data operations read requires administrator authority',
      );
    }
  }

  /** 保证预检只允许超级管理员调用。 */
  private assertWriter(actor: AuthContext): void {
    if (actor.role !== Role.SUPER_ADMIN) {
      throw new PublicProblemException(
        HttpStatus.FORBIDDEN,
        'forbidden',
        'Data operations write requires super-administrator authority',
      );
    }
  }

  /** 投影内部总览，唯一执行槽仍完全由 data-sync 权威返回。 */
  private overviewProjection(value: unknown): Record<string, unknown> {
    const record = asRecord(value);
    if (!record) throw unavailable();
    return {
      datasetCount: requiredField(record, 'datasetCount'),
      enabledDatasetCount: requiredField(record, 'enabledDatasetCount'),
      healthSummary: pickObject(record.healthSummary, [
        'status',
        'score',
        'evaluatedAt',
        'evaluationId',
        'warningCount',
        'criticalCount',
        'openIssueCount',
        'affectedRecordCount',
      ]),
      executionSlot: this.overviewSlot(record.executionSlot),
      queuedRunCount: requiredField(record, 'queuedRunCount'),
      failedRunCount24h: requiredField(record, 'failedRunCount24h'),
      generatedAt: requiredField(record, 'generatedAt'),
    };
  }

  /** 投影执行槽公开字段，既不复制也不计算租约状态。 */
  private overviewSlot(value: unknown): Record<string, unknown> {
    return pickObject(value, ['state', 'runId', 'datasetCode', 'leaseUntil', 'heartbeatAt']);
  }

  /** 投影数据目录摘要，显式裁掉只属于内部 schema 的字段。 */
  private datasetSummary(value: unknown): Record<string, unknown> {
    const record = asRecord(value);
    const sourceBindings = record === null ? null : boundedArray(record, 'sourceBindings', 10);
    if (!record || !sourceBindings) throw unavailable();
    return {
      datasetCode: requiredField(record, 'datasetCode'),
      displayName: requiredField(record, 'displayName'),
      domain: requiredField(record, 'domain'),
      lifecycleStatus: requiredField(record, 'lifecycleStatus'),
      availability: requiredField(record, 'availability'),
      availabilityReasonCode: nullableField(record, 'availabilityReasonCode'),
      observationState: requiredField(record, 'observationState'),
      observationStateReasonCode: nullableField(record, 'observationStateReasonCode'),
      sourceBindings: sourceBindings.map(sourceBinding),
      capability: datasetCapability(record.capability),
      timing: datasetTiming(record.timing),
      latestRun: record.latestRun === null ? null : this.runSummary(record.latestRun),
      healthSummary: healthSummary(record.healthSummary),
      scheduleSummary:
        record.scheduleSummary === null ? null : scheduleSummary(record.scheduleSummary),
    };
  }

  /** 投影运行摘要并仅保留合同允许的 ErrorSummary。 */
  private runSummary(value: unknown): Record<string, unknown> {
    const record = asRecord(value);
    if (!record) throw unavailable();
    return {
      runId: requiredField(record, 'runId'),
      commandId: requiredField(record, 'commandId'),
      datasetCode: requiredField(record, 'datasetCode'),
      mode: requiredField(record, 'mode'),
      status: requiredField(record, 'status'),
      queuePosition: nullableField(record, 'queuePosition'),
      requestedAt: requiredField(record, 'requestedAt'),
      startedAt: nullableField(record, 'startedAt'),
      finishedAt: nullableField(record, 'finishedAt'),
      progress: runProgress(record.progress),
      error: safeError(record.error),
    };
  }

  /** 将内部运行时间线事件转为不含 actorRef 的公开 OperationEventView。 */
  private async operationEvent(value: unknown): Promise<Record<string, unknown>> {
    const record = asRecord(value);
    if (!record) throw unavailable();
    const actorRef = requiredString(record, 'actorRef');
    if (!actorRef) throw unavailable();
    return {
      eventId: requiredField(record, 'eventId'),
      resourceType: requiredField(record, 'resourceType'),
      resourceId: requiredField(record, 'resourceId'),
      action: requiredField(record, 'action'),
      result: requiredField(record, 'result'),
      actor: await this.projection.actorDisplay(actorRef, null),
      requestId: requiredField(record, 'requestId'),
      occurredAt: requiredField(record, 'occurredAt'),
      error: safeError(record.error),
    };
  }

  /** 将内部计划实体扁平为公开 ScheduleView 并屏蔽 updatedByActorRef。 */
  private async scheduleView(value: unknown): Promise<Record<string, unknown>> {
    const record = asRecord(value);
    const summary = record === null ? null : asRecord(record.summary);
    const actorRef = record === null ? null : requiredString(record, 'updatedByActorRef');
    const datasetCode = record === null ? null : requiredString(record, 'datasetCode');
    if (!record || !summary || !actorRef || !datasetCode) throw unavailable();
    return {
      scheduleId: requiredField(summary, 'scheduleId'),
      datasetCode,
      mode: requiredField(record, 'mode'),
      selector: strictTargetSelector(datasetCode, requiredField(record, 'selector')),
      targetPolicy: strictScheduleTargetPolicy(record.targetPolicy),
      enabled: requiredField(summary, 'enabled'),
      frequency: strictScheduleFrequency(summary.frequency),
      misfirePolicy: requiredField(record, 'misfirePolicy'),
      coalesce: requiredField(record, 'coalesce'),
      nextRunAt: nullableField(summary, 'nextRunAt'),
      nextOccurrences: boundedStringArray(record, 'nextOccurrences', 10),
      version: requiredField(summary, 'version'),
      updatedAt: requiredField(record, 'updatedAt'),
      updatedBy: await this.projection.actorDisplay(actorRef, null),
    };
  }
}

/** 从内部 SourceBinding 明确挑选允许公开的 Provider 与真实上游来源字段。 */
function sourceBinding(value: unknown): Record<string, unknown> {
  return pickObject(value, [
    'providerId',
    'upstreamSource',
    'sourceDataset',
    'adapterId',
    'methodologyCode',
    'methodologyVersion',
    'approvalStatus',
    'role',
    'effective',
  ]);
}

/** 从内部 SyncTarget 挑选四种模式允许的日期字段，并可要求 ETF 全量版本已冻结。 */
function syncTarget(value: unknown, requireFrozenEtfProfiles = false): Record<string, unknown> {
  const record = asRecord(value);
  if (
    !record ||
    !('dateFrom' in record) ||
    !('dateTo' in record) ||
    !('observationDate' in record)
  ) {
    throw unavailable();
  }
  const parsed = (requireFrozenEtfProfiles ? syncFrozenTargetSchema : syncTargetSchema).safeParse(
    record,
  );
  if (!parsed.success) throw unavailable();
  return {
    datasetCode: parsed.data.datasetCode,
    mode: parsed.data.mode,
    selector: parsed.data.selector,
    dateFrom: parsed.data.dateFrom ?? null,
    dateTo: parsed.data.dateTo ?? null,
    observationDate: parsed.data.observationDate ?? null,
  };
}

/** 投影预检 target，并让嵌套同步目标经过与公开写请求相同的严格校验。 */
function preflightTarget(value: unknown): Record<string, unknown> {
  const record = asRecord(value);
  if (!record) throw unavailable();
  return {
    target: syncTarget(record.target, true),
    eligible: booleanField(record, 'eligible'),
    estimatedPartitions: nonNegativeInteger(record, 'estimatedPartitions'),
    estimatedProviderCalls: nonNegativeInteger(record, 'estimatedProviderCalls'),
    resolvedDateFrom: nullableStringField(record, 'resolvedDateFrom'),
    resolvedDateTo: nullableStringField(record, 'resolvedDateTo'),
    warnings: boundedStringArray(record, 'warnings', 20),
  };
}

/** 只投影数据集能力的封闭公开字段，并限制每个枚举数组避免合同漂移扩张响应。 */
function datasetCapability(value: unknown): Record<string, unknown> {
  const record = asRecord(value);
  if (!record) throw unavailable();
  return {
    supportedModes: boundedStringArray(record, 'supportedModes', 4),
    scheduleSupportedModes: boundedStringArray(record, 'scheduleSupportedModes', 3),
    scheduleTargetPolicyOptions: boundedArray(record, 'scheduleTargetPolicyOptions', 10).map(
      scheduleTargetPolicyOption,
    ),
    selectorKinds: boundedStringArray(record, 'selectorKinds', 11),
    maxRangeDays: nullableNonNegativeInteger(record, 'maxRangeDays'),
    scheduleEligible: booleanField(record, 'scheduleEligible'),
    manualEnabled: booleanField(record, 'manualEnabled'),
    correctionLookbackDays: nonNegativeInteger(record, 'correctionLookbackDays'),
  };
}

/** 投影一个带模式、嵌套策略和默认标识的计划目标策略选项。 */
function scheduleTargetPolicyOption(value: unknown): Record<string, unknown> {
  const option = asRecord(value);
  if (!option) throw unavailable();
  return {
    mode: requiredField(option, 'mode'),
    policy: pickObject(option.policy, ['policyVersion', 'dateResolution']),
    isDefault: booleanField(option, 'isDefault'),
  };
}

/** 只投影数据集时间与 freshness 事实，不接受内部 cursor 或额外 provider 字段。 */
function datasetTiming(value: unknown): Record<string, unknown> {
  return pickObject(value, [
    'lastAttemptStartedAt',
    'lastAttemptFinishedAt',
    'lastAttemptStatus',
    'lastSuccessAt',
    'lastPublishedAt',
    'dataAsOf',
    'dataAsOfKind',
    'dataAsOfLabel',
    'coverageFrom',
    'coverageTo',
    'freshnessStatus',
    'freshnessLagValue',
    'freshnessLagUnit',
    'freshnessReasonCode',
    'freshnessEvaluatedAt',
  ]);
}

/** 投影健康摘要，禁止把评估证据或规则样本从目录摘要透出。 */
function healthSummary(value: unknown): Record<string, unknown> {
  return pickObject(value, [
    'status',
    'score',
    'evaluatedAt',
    'evaluationId',
    'warningCount',
    'criticalCount',
    'openIssueCount',
    'affectedRecordCount',
  ]);
}

/** 投影计划摘要，只保留下一次触发与乐观版本。 */
function scheduleSummary(value: unknown): Record<string, unknown> {
  return pickObject(value, ['scheduleId', 'enabled', 'frequency', 'nextRunAt', 'version']);
}

/** 投影运行进度的计数与可空估算，不允许仓储 checkpoint 进入摘要。 */
function runProgress(value: unknown): Record<string, unknown> {
  const record = asRecord(value);
  if (!record) throw unavailable();
  return {
    completedPartitions: nonNegativeInteger(record, 'completedPartitions'),
    totalPartitions: nonNegativeInteger(record, 'totalPartitions'),
    processedRecords: nonNegativeInteger(record, 'processedRecords'),
    estimatedRecords: nullableNonNegativeInteger(record, 'estimatedRecords'),
  };
}

/** 校验计划 selector 与数据集固定操作完全一致，拒绝 Provider 参数或任意扩展字段。 */
function strictTargetSelector(datasetCode: string, value: unknown): unknown {
  const parsed = draftTargetSelectorSchema.safeParse(value);
  if (!parsed.success || !targetSelectorMatchesDataset(datasetCode, parsed.data)) {
    throw unavailable();
  }
  return parsed.data;
}

/** 校验计划目标日期策略的有限枚举。 */
function strictScheduleTargetPolicy(value: unknown): unknown {
  const parsed = scheduleTargetPolicySchema.safeParse(value);
  if (!parsed.success) throw unavailable();
  return parsed.data;
}

/** 校验计划结构化频率并要求所有可空字段显式返回，避免浏览器猜测缺失值。 */
function strictScheduleFrequency(value: unknown): Record<string, unknown> {
  const record = asRecord(value);
  if (
    !record ||
    ![
      'kind',
      'timezone',
      'localTime',
      'dayOfWeek',
      'dayOfMonth',
      'intervalMinutes',
      'calendarCode',
    ].every((key) => key in record)
  ) {
    throw unavailable();
  }
  const parsed = scheduleFrequencySchema.safeParse(record);
  if (!parsed.success) throw unavailable();
  return pickRecord(parsed.data, [
    'kind',
    'timezone',
    'localTime',
    'dayOfWeek',
    'dayOfMonth',
    'intervalMinutes',
    'calendarCode',
  ]);
}

/** 读取长度受合同上限约束的数组，过大的内部响应不能放大公开响应。 */
function boundedArray(record: Record<string, unknown>, key: string, maximum: number): unknown[] {
  const values = requiredArray(record, key);
  if (!values || values.length > maximum) throw unavailable();
  // 调用方随后按 DTO 白名单逐项投影；这里不能扫描原项，否则包含 checkpoint 的内部 partition
  // 会在安全字段被裁掉前失败，破坏“拒绝泄漏而非拒绝合法详情”的兼容边界。
  return values;
}

/** 读取长度和单项长度均受限的字符串数组。 */
function boundedStringArray(
  record: Record<string, unknown>,
  key: string,
  maximum: number,
): string[] {
  return boundedArray(record, key, maximum).map((value) => {
    if (typeof value !== 'string' || value.length === 0 || value.length > 4096) {
      throw unavailable();
    }
    return value;
  });
}

/** 读取合同要求的布尔字段，拒绝将数字或字符串宽松转换为布尔值。 */
function booleanField(record: Record<string, unknown>, key: string): boolean {
  const value = requiredField(record, key);
  if (typeof value !== 'boolean') throw unavailable();
  return value;
}

/** 读取非负安全整数，避免出现负进度或精度已丢失的计数。 */
function nonNegativeInteger(record: Record<string, unknown>, key: string): number {
  const value = requiredField(record, key);
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw unavailable();
  }
  return value;
}

/** 读取可空非负安全整数，保留合同定义的未知估算值。 */
function nullableNonNegativeInteger(record: Record<string, unknown>, key: string): number | null {
  const value = nullableField(record, key);
  if (value === null) return null;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw unavailable();
  }
  return value;
}

/** 读取可空字符串字段，避免意外把嵌套对象投影到公开 DTO。 */
function nullableStringField(record: Record<string, unknown>, key: string): string | null {
  const value = nullableField(record, key);
  if (value === null) return null;
  if (typeof value !== 'string' || value.length === 0 || value.length > 4096) {
    throw unavailable();
  }
  return value;
}

/** 从内部质量门摘要挑选稳定安全字段。 */
function qualityGate(value: unknown): Record<string, unknown> {
  const record = asRecord(value);
  if (!record) throw unavailable();
  return {
    disposition: requiredField(record, 'disposition'),
    policyCode: nullableField(record, 'policyCode'),
    policyVersion: nullableField(record, 'policyVersion'),
    affectedCount: nullableField(record, 'affectedCount'),
    error: safeError(record.error),
  };
}

/** 从 ErrorSummary 只保留四个公开稳定字段。 */
function safeError(value: unknown): SafeError | null {
  if (value === null) return null;
  const record = asRecord(value);
  if (!record) throw unavailable();
  const code = typeof record.code === 'string' ? record.code : null;
  const stage = typeof record.stage === 'string' ? record.stage : null;
  const retryable = typeof record.retryable === 'boolean' ? record.retryable : null;
  const message = typeof record.message === 'string' ? record.message : null;
  if (
    !code ||
    code.length > 80 ||
    !stage ||
    !ERROR_STAGES.has(stage as SafeError['stage']) ||
    retryable === null ||
    !message ||
    message.length > 500
  ) {
    throw unavailable();
  }
  return { code, stage: stage as SafeError['stage'], retryable, message };
}

/** 按白名单复制内部对象字段；未知字段永远不进入公开响应。 */
function pickObject(value: unknown, keys: readonly string[]): Record<string, unknown> {
  const record = asRecord(value);
  if (!record) throw unavailable();
  return pickRecord(record, keys);
}

/** 按白名单复制已验证对象的字段，保留合同明确要求的 null。 */
function pickRecord(
  record: Record<string, unknown>,
  keys: readonly string[],
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const key of keys) {
    if (!(key in record)) throw unavailable();
    result[key] = publicValue(record[key]);
  }
  return result;
}

/** 读取必填合同字段，缺失表示内部响应与版本化合同漂移。 */
function requiredField(record: Record<string, unknown>, key: string): unknown {
  if (!(key in record)) throw unavailable();
  return publicValue(record[key]);
}

/** 读取可空合同字段，缺失同样视为合同漂移。 */
function nullableField(record: Record<string, unknown>, key: string): unknown {
  if (!(key in record)) throw unavailable();
  return publicValue(record[key]);
}

/**
 * 对白名单字段的值再做深度校验，阻断内部游标、凭据和堆栈经嵌套对象泄漏。
 * 该函数只处理 JSON 基元与有界容器，data-sync 的非 JSON 返回一律视为合同漂移。
 */
function publicValue(value: unknown, depth = 0): unknown {
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw unavailable();
    return value;
  }
  if (typeof value === 'string') {
    if (value.length > 4096) throw unavailable();
    return value;
  }
  if (depth >= 6) throw unavailable();
  if (Array.isArray(value)) {
    if (value.length > 200) throw unavailable();
    return value.map((item) => publicValue(item, depth + 1));
  }
  const record = asRecord(value);
  if (!record || Object.keys(record).length > 50) throw unavailable();
  const projected: Record<string, unknown> = {};
  for (const [key, nested] of Object.entries(record)) {
    if (isInternalValueKey(key)) throw unavailable();
    projected[key] = publicValue(nested, depth + 1);
  }
  return projected;
}

/** 归一化内部字段的命名变体，防止 snake_case 或连字符写法绕过脱敏规则。 */
function isInternalValueKey(key: string): boolean {
  const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '');
  if (INTERNAL_VALUE_KEYS.has(normalized)) return true;
  return (
    [
      'credential',
      'secret',
      'token',
      'password',
      'authorization',
      'fencing',
      'checkpoint',
      'cursor',
      'stack',
      'traceback',
    ].some((fragment) => normalized.includes(fragment)) ||
    (normalized.includes('provider') &&
      (normalized.includes('payload') || normalized.includes('response'))) ||
    normalized.endsWith('uri') ||
    normalized.endsWith('url')
  );
}

/** 表示 ErrorSummary 合同允许的稳定阶段值。 */
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

/** 表示任何公开 DTO 中均不得出现的内部字段名。 */
const INTERNAL_VALUE_KEYS = new Set([
  'actorref',
  'fencingtoken',
  'checkpoint',
  'providercursor',
  'providerpayload',
  'rawpayload',
  'rawresponse',
  'credential',
  'credentials',
  'authorization',
  'token',
  'secret',
  'stack',
  'stacktrace',
  'traceback',
  'executionintent',
  'legacyintent',
  'uri',
  'url',
]);

/** 返回不包含内部响应正文的公开依赖故障。 */
function unavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Data operations are temporarily unavailable',
  );
}
