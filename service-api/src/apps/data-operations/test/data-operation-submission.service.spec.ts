/* eslint-disable @typescript-eslint/no-unnecessary-type-assertion, @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-return, @typescript-eslint/require-await */

import { Role, type DataOperationSubmission } from '../../../generated/prisma/client.js';
import { createHash } from 'node:crypto';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { AppConfigService } from '../../../config/app-config.service.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import type { DataOperationsWriteRequest } from '../../../data-sync/contracts/data-operations.contract.js';
import { describe, expect, it, vi } from 'vitest';

import { DataOperationSubmissionService } from '../data-operation-submission.service.js';
import { canonicalJson } from '../data-operations.types.js';

/** 提供可用于幂等、outbox 与审计测试的超级管理员身份。 */
const superActor: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000002',
  role: Role.SUPER_ADMIN,
  securityVersion: 1,
};

/** 提供 actorRef HMAC 所需的最小应用配置。 */
const config = {
  jwtAccessSecret: 'test-only-data-operations-submission-secret-long-enough',
} as AppConfigService;

/** 覆盖 Submission、Outbox、AuditLog 的原子写入以及公开幂等隔离。 */
describe('DataOperationSubmissionService', () => {
  /** 验证首次写只返回 PENDING，并在同一事务保存 Submission、冻结 outbox 和审计。 */
  it('persists submission outbox and audit atomically with the initial pending invariant', async () => {
    const created = submissionRow();
    const create = vi.fn().mockImplementation(async ({ data }: { data: { id: string } }) => ({
      ...created,
      id: data.id,
    }));
    const outboxCreate = vi.fn().mockResolvedValue({});
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      dataOperationSubmission: { create },
      apiOutbox: { create: outboxCreate },
      auditLog: { create: auditCreate },
    };
    const findUnique = vi.fn().mockResolvedValue(null);
    const service = submissionService({
      client: {
        dataOperationSubmission: { findUnique },
        $transaction: vi.fn(async (callback) => callback(transaction)),
      },
    });

    const receipt = await service.submit(
      superActor,
      'SYNC_SUBMIT',
      syncSubmitRequest(),
      'public-idempotency-key-0001',
      'request-submit-1',
    );
    const firstCreateCall = create.mock.calls[0];
    if (!firstCreateCall) throw new Error('Expected submission create call');
    const persistedSubmissionId = (firstCreateCall[0] as { data: { id: string } }).data.id;

    expect(receipt).toMatchObject({
      submissionId: persistedSubmissionId,
      action: 'SYNC_SUBMIT',
      deliveryStatus: 'PENDING',
      operationResult: 'UNKNOWN',
      authorityResource: null,
      queuePosition: null,
    });
    expect(create).toHaveBeenCalledTimes(1);
    expect(outboxCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          submissionId: persistedSubmissionId,
          downstreamIdempotencyKey: `dataops:${persistedSubmissionId}`,
          internalPath: '/internal/v1/data-operations/commands/submit',
          canonicalPayload: expect.objectContaining({
            submissionId: persistedSubmissionId,
            targets: [expect.objectContaining({ selector: { kind: 'GLOBAL' } })],
          }),
        }),
      }),
    );
    expect(JSON.stringify(outboxCreate.mock.calls)).not.toContain('public-idempotency-key-0001');
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          action: 'dataops.request.authorized',
          targetId: persistedSubmissionId,
        }),
      }),
    );
  });

  /** 验证相同 actor 与相同 key/请求直接复用原 Submission，绝不创建第二个业务命令。 */
  it('returns the original receipt for an identical idempotent request', async () => {
    const existing = submissionRow({ deliveryStatus: 'ACCEPTED', operationResult: 'QUEUED' });
    const service = submissionService({
      client: { dataOperationSubmission: { findUnique: vi.fn().mockResolvedValue(existing) } },
    });

    const receipt = await service.submit(
      superActor,
      'SYNC_SUBMIT',
      syncSubmitRequest(),
      'public-idempotency-key-0001',
      'request-submit-2',
    );

    expect(receipt).toMatchObject({ submissionId: existing.id, deliveryStatus: 'ACCEPTED' });
  });

  /** 验证相同 actor/key 配合不同语义请求明确返回 409，而不会覆盖冻结 outbox。 */
  it('rejects a different request that reuses the same actor idempotency key', async () => {
    const existing = submissionRow();
    const service = submissionService({
      client: { dataOperationSubmission: { findUnique: vi.fn().mockResolvedValue(existing) } },
    });

    await expect(
      service.submit(
        superActor,
        'SYNC_SUBMIT',
        { ...syncSubmitRequest(), reason: '改成另一个业务原因' },
        'public-idempotency-key-0001',
        'request-submit-3',
      ),
    ).rejects.toMatchObject({ status: 409 });
  });

  /** 验证服务层自身拒绝 ADMIN 写入，避免测试或后台调用绕过 Controller 权限。 */
  it('rejects non-super-administrator writes before touching storage', async () => {
    const findUnique = vi.fn();
    const service = submissionService({ client: { dataOperationSubmission: { findUnique } } });

    await expect(
      service.submit(
        { ...superActor, role: Role.ADMIN },
        'SYNC_SUBMIT',
        syncSubmitRequest(),
        'public-idempotency-key-0001',
        'request-submit-4',
      ),
    ).rejects.toMatchObject({ status: 403 });
    expect(findUnique).not.toHaveBeenCalled();
  });

  /** 验证超过批准的 64 KiB 冻结 payload 上限时，不会开始数据库授权事务。 */
  it('rejects an outbox payload larger than 64 KiB before persistence', async () => {
    const transaction = vi.fn();
    const service = submissionService({
      client: {
        dataOperationSubmission: { findUnique: vi.fn().mockResolvedValue(null) },
        $transaction: transaction,
      },
    });
    const largeRequest = {
      ...syncSubmitRequest(),
      targets: Array.from({ length: 100 }, (_, index) => ({
        datasetCode: `equity.daily.${index}`,
        mode: 'FULL' as const,
        selector: {
          kind: 'INSTRUMENT' as const,
          exchange: 'SSE' as const,
          symbol: 'X'.repeat(1_024),
        },
        dateFrom: null,
        dateTo: null,
        observationDate: null,
      })),
    } as DataOperationsWriteRequest;

    await expect(
      service.submit(
        superActor,
        'SYNC_SUBMIT',
        largeRequest,
        'public-idempotency-key-large',
        'request-submit-large',
      ),
    ).rejects.toMatchObject({ status: 422 });
    expect(transaction).not.toHaveBeenCalled();
  });
});

/** 构造满足公开收据状态机的最小 Submission 数据库行。 */
function submissionRow(overrides: Partial<DataOperationSubmission> = {}): DataOperationSubmission {
  return {
    id: '00000000-0000-4000-8000-000000000010',
    actorId: superActor.userId,
    actorRole: Role.SUPER_ADMIN,
    action: 'SYNC_SUBMIT',
    idempotencyKey: 'public-idempotency-key-0001',
    requestHash: syncSubmitRequestHash(),
    sanitizedRequest: syncSubmitRequest(),
    actorRef: 'user:opaque-reference',
    reason: '补齐历史同步',
    deliveryStatus: 'PENDING',
    operationResult: 'UNKNOWN',
    authorityType: null,
    authorityId: null,
    queuePosition: null,
    requestId: 'request-submit-1',
    safeError: null,
    version: 1,
    authorizedAt: new Date('2026-07-29T00:00:00.000Z'),
    updatedAt: new Date('2026-07-29T00:00:00.000Z'),
    completedAt: null,
    lastObservedAt: null,
    ...overrides,
  } as unknown as DataOperationSubmission;
}

/** 构造预检已冻结的最小同步提交请求，包含强制 selector。 */
function syncSubmitRequest(): DataOperationsWriteRequest {
  return {
    preflightId: '00000000-0000-4000-8000-000000000020',
    requestHash: 'a'.repeat(64),
    targets: [
      {
        datasetCode: 'equity.daily',
        mode: 'FULL',
        selector: { kind: 'GLOBAL' },
        dateFrom: null,
        dateTo: null,
        observationDate: null,
      },
    ],
    reason: '补齐历史同步',
  } as DataOperationsWriteRequest;
}

/** 以生产服务相同的规范 JSON 算法构造幂等语义摘要。 */
function syncSubmitRequestHash(): string {
  return createHash('sha256')
    .update(`SYNC_SUBMIT:${canonicalJson(syncSubmitRequest())}`)
    .digest('hex');
}

/** 注入最小 Prisma 替身，保持每个测试只声明实际使用的数据库方法。 */
function submissionService(database: unknown): DataOperationSubmissionService {
  return new DataOperationSubmissionService(database as DatabaseService, config);
}
