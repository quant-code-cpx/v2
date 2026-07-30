import { Role } from '../../../generated/prisma/client.js';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { DataOperationsClient } from '../../../data-sync/clients/data-operations.client.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import { describe, expect, it, vi } from 'vitest';

import { DataOperationsQueryService } from '../data-operations-query.service.js';
import type { DataOperationsProjectionService } from '../data-operations-projection.service.js';

/** 提供可读取数据运维资源的测试管理员。 */
const reader: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000002',
  role: Role.ADMIN,
  securityVersion: 1,
};

/** 提供可执行预检的数据运维超级管理员。 */
const writer: AuthContext = {
  ...reader,
  role: Role.SUPER_ADMIN,
};

/** 覆盖权威读取投影中的 selector 能力与内部敏感字段移除。 */
describe('DataOperationsQueryService', () => {
  /** 验证 DatasetCapability 将 selector 与嵌套计划策略按公开合同完整投影。 */
  it('projects dataset capability selector kinds', async () => {
    const client = {
      searchDatasets: vi.fn().mockResolvedValue({
        items: [datasetSummary()],
        nextCursor: null,
        totalEstimate: 1,
        generatedAt: '2026-07-29T00:00:00.000Z',
      }),
    } as unknown as DataOperationsClient;
    const service = queryService(client);

    const result = await service.searchDatasets(reader, {}, 'dataset-request-1');

    expect((result.items as Array<Record<string, unknown>>)[0]?.capability).toMatchObject({
      selectorKinds: ['GLOBAL', 'INSTRUMENT'],
      scheduleTargetPolicyOptions: [
        {
          mode: 'INCREMENTAL',
          policy: {
            policyVersion: 1,
            dateResolution: 'NONE',
          },
          isDefault: true,
        },
      ],
    });
  });

  /** 验证全量 ETF 预检只返回已冻结的沪深 profile publication，不能降级成未冻结草稿。 */
  it('projects the frozen ETF profile publications from preflight', async () => {
    const target = {
      datasetCode: 'fund.etf.nav.1d.reported',
      mode: 'INCREMENTAL',
      selector: {
        kind: 'ETF',
        operation: 'NAV',
        venue: null,
        scope: 'ALL_ETFS',
        etf: null,
        profileDataVersions: {
          SSE: '00000000-0000-4000-8000-000000000011',
          SZSE: '00000000-0000-4000-8000-000000000012',
        },
      },
      dateFrom: null,
      dateTo: null,
      observationDate: null,
    };
    const client = {
      preflight: vi.fn().mockResolvedValue({
        preflightId: '00000000-0000-4000-8000-000000000020',
        requestHash: 'a'.repeat(64),
        expiresAt: '2026-07-30T12:00:00.000Z',
        queueDepth: 0,
        executionSlot: {
          state: 'IDLE',
          runId: null,
          datasetCode: null,
          leaseUntil: null,
          heartbeatAt: null,
        },
        targets: [
          {
            target,
            eligible: true,
            estimatedPartitions: 2,
            estimatedProviderCalls: 2,
            resolvedDateFrom: null,
            resolvedDateTo: null,
            warnings: [],
          },
        ],
        accepted: true,
      }),
    } as unknown as DataOperationsClient;

    const result = await queryService(client).preflight(writer, { targets: [] }, 'preflight-1');

    expect(result.targets).toEqual([
      {
        target,
        eligible: true,
        estimatedPartitions: 2,
        estimatedProviderCalls: 2,
        resolvedDateFrom: null,
        resolvedDateTo: null,
        warnings: [],
      },
    ]);
  });

  /** 验证 RunDetail 只展示 ActorDisplay 和脱敏摘要，不泄漏 actorRef、fencingToken 或 checkpoint。 */
  it('strips internal run execution fields from the public view', async () => {
    const actorDisplay = vi.fn().mockResolvedValue({
      actorType: 'USER',
      systemKind: null,
      actorId: reader.userId,
      displayName: '运维管理员',
      deleted: false,
    });
    const client = {
      getRun: vi.fn().mockResolvedValue(runDetailWithInternalFields()),
    } as unknown as DataOperationsClient;
    const service = queryService(client, {
      actorDisplay,
    } as unknown as DataOperationsProjectionService);

    const result = await service.getRun(
      reader,
      {
        runId: '00000000-0000-4000-8000-000000000010',
        partitionsCursor: null,
        timelineCursor: null,
      },
      'run-request-1',
    );

    const serialized = JSON.stringify(result);
    expect(serialized).not.toContain('user:opaque-reference');
    expect(serialized).not.toContain('987654321');
    expect(serialized).not.toContain('provider-cursor-secret');
    expect(result.target).toMatchObject({ selector: { kind: 'GLOBAL' } });
    expect(result.requestedBy).toMatchObject({ displayName: '运维管理员' });
  });

  /** 验证内部响应若夹带 Provider 游标，白名单投影会裁掉该字段而不是泄漏给浏览器。 */
  it('drops a nested provider cursor instead of leaking it', async () => {
    const summary = datasetSummary();
    const bindings = summary.sourceBindings as Array<Record<string, unknown>>;
    bindings[0]!.provider_cursor = 'sensitive-provider-cursor';
    const client = {
      searchDatasets: vi.fn().mockResolvedValue({
        items: [summary],
        nextCursor: null,
        totalEstimate: 1,
        generatedAt: '2026-07-29T00:00:00.000Z',
      }),
    } as unknown as DataOperationsClient;

    const result = await queryService(client).searchDatasets(reader, {}, 'dataset-request-2');

    expect(JSON.stringify(result)).not.toContain('sensitive-provider-cursor');
  });
});

/** 构造包含 selectorKinds 的最小数据集摘要，覆盖合同所需的所有公开字段。 */
function datasetSummary(): Record<string, unknown> {
  return {
    datasetCode: 'equity.daily',
    displayName: '股票日线',
    domain: 'equity',
    lifecycleStatus: 'PRODUCTION',
    availability: 'ENABLED',
    availabilityReasonCode: null,
    observationState: 'PRESENT',
    observationStateReasonCode: null,
    sourceBindings: [sourceBinding()],
    capability: {
      supportedModes: ['FULL', 'INCREMENTAL'],
      scheduleSupportedModes: ['INCREMENTAL'],
      scheduleTargetPolicyOptions: [
        {
          mode: 'INCREMENTAL',
          policy: {
            policyVersion: 1,
            dateResolution: 'NONE',
          },
          isDefault: true,
        },
      ],
      selectorKinds: ['GLOBAL', 'INSTRUMENT'],
      maxRangeDays: null,
      scheduleEligible: true,
      manualEnabled: true,
      correctionLookbackDays: 5,
    },
    timing: {
      lastAttemptStartedAt: null,
      lastAttemptFinishedAt: null,
      lastAttemptStatus: null,
      lastSuccessAt: null,
      lastPublishedAt: null,
      dataAsOf: null,
      dataAsOfKind: null,
      dataAsOfLabel: null,
      coverageFrom: null,
      coverageTo: null,
      freshnessStatus: 'UNKNOWN',
      freshnessLagValue: null,
      freshnessLagUnit: null,
      freshnessReasonCode: null,
      freshnessEvaluatedAt: '2026-07-29T00:00:00.000Z',
    },
    latestRun: null,
    healthSummary: {
      status: 'UNKNOWN',
      score: null,
      evaluatedAt: null,
      evaluationId: null,
      warningCount: 0,
      criticalCount: 0,
      openIssueCount: 0,
      affectedRecordCount: null,
    },
    scheduleSummary: null,
  };
}

/** 构造不含任何 Provider 原文的公开 SourceBinding。 */
function sourceBinding(): Record<string, unknown> {
  return {
    providerId: 'akshare',
    upstreamSource: 'Eastmoney',
    sourceDataset: 'stock_zh_a_hist',
    adapterId: 'akshare-equity',
    methodologyCode: 'raw',
    methodologyVersion: 1,
    approvalStatus: 'APPROVED',
    role: 'PRIMARY',
    effective: true,
  };
}

/** 构造同时含内部执行字段的 data-sync RunDetail，用于验证白名单投影。 */
function runDetailWithInternalFields(): Record<string, unknown> {
  return {
    run: {
      runId: '00000000-0000-4000-8000-000000000010',
      commandId: '00000000-0000-4000-8000-000000000011',
      datasetCode: 'equity.daily',
      mode: 'FULL',
      status: 'RUNNING',
      queuePosition: 1,
      requestedAt: '2026-07-29T00:00:00.000Z',
      startedAt: '2026-07-29T00:00:01.000Z',
      finishedAt: null,
      progress: {
        completedPartitions: 0,
        totalPartitions: 1,
        processedRecords: 0,
        estimatedRecords: null,
      },
      error: null,
    },
    target: {
      datasetCode: 'equity.daily',
      mode: 'FULL',
      selector: { kind: 'GLOBAL' },
      dateFrom: null,
      dateTo: null,
      observationDate: null,
    },
    sourceSnapshot: [sourceBinding()],
    attempt: 1,
    actorRef: 'user:opaque-reference',
    qualityGate: {
      disposition: 'NOT_EVALUATED',
      policyCode: null,
      policyVersion: null,
      affectedCount: null,
      error: null,
    },
    partitionCount: 1,
    partitions: [
      {
        partitionKey: '2026-07-29',
        status: 'RUNNING',
        attempt: 1,
        checkpoint: { summary: 'provider-cursor-secret' },
        error: null,
      },
    ],
    partitionsNextCursor: null,
    timelineEventCount: 0,
    timeline: [],
    timelineNextCursor: null,
    fencingToken: 987654321,
  };
}

/** 注入查询投影所需的最小 client、数据库与 ActorDisplay 替身。 */
function queryService(
  client: DataOperationsClient,
  projection = { actorDisplay: vi.fn() } as unknown as DataOperationsProjectionService,
): DataOperationsQueryService {
  return new DataOperationsQueryService(
    client,
    { client: { dataOperationSubmission: { count: vi.fn() } } } as unknown as DatabaseService,
    projection,
  );
}
