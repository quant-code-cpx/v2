/* eslint-disable @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-return, @typescript-eslint/require-await, @typescript-eslint/unbound-method */

import {
  ApiOutboxState,
  DataOperationDeliveryStatus,
  Role,
  UserStatus,
} from '../../../generated/prisma/client.js';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { AppConfigService } from '../../../config/app-config.service.js';
import type { DataOperationsClient } from '../../../data-sync/clients/data-operations.client.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import { describe, expect, it, vi } from 'vitest';

import {
  DEAD_LETTER_REPLAY_CONFIRMATION,
  DataOperationOutboxDispatcher,
} from '../data-operation-outbox.dispatcher.js';

/** 提供受控 replay 所需的活动超级管理员快照。 */
const replayActor: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000002',
  role: Role.SUPER_ADMIN,
  securityVersion: 1,
};

/** 提供 HMAC 审计摘要所需的最小服务端密钥配置。 */
const config = {
  jwtAccessSecret: 'test-only-data-operations-outbox-secret-long-enough',
} as AppConfigService;

/** 覆盖可靠 outbox 的 PostgreSQL 领取、稳定内部 key 和 cancel_too_late 投影。 */
describe('DataOperationOutboxDispatcher', () => {
  /** 验证并发 dispatcher 领取 SQL 采用 FOR UPDATE SKIP LOCKED，空队列不触发网络投递。 */
  it('claims through PostgreSQL skip locked instead of a process-local lock', async () => {
    const queryRaw = vi.fn().mockResolvedValue([]);
    const dispatcher = dispatcherWith({
      client: {
        $transaction: vi.fn(async (callback) =>
          callback({
            $queryRaw: queryRaw,
            apiOutbox: { findMany: vi.fn() },
            dataOperationSubmission: { updateMany: vi.fn() },
          }),
        ),
      },
    });

    await expect(dispatcher.dispatchOnce('worker-a', 1)).resolves.toBe(0);
    expect(String(queryRaw.mock.calls[0]?.[0])).toContain('FOR UPDATE SKIP LOCKED');
  });

  /** 验证过期 worker 不再拥有 outbox lease 时不能覆盖较新交付结果。 */
  it('does not update submission when the delivery lease owner changed', async () => {
    const updateMany = vi.fn().mockResolvedValue({ count: 0 });
    const submissionUpdate = vi.fn();
    const dispatcher = dispatcherWith({
      client: {
        $transaction: vi.fn(async (callback) =>
          callback({
            apiOutbox: { updateMany },
            dataOperationSubmission: { update: submissionUpdate },
            auditLog: { create: vi.fn() },
          }),
        ),
      },
    });

    await privateMarkAccepted(dispatcher, cancelOutbox(), 'stale-worker', {
      authority: { resourceType: 'COMMAND', resourceId: '00000000-0000-4000-8000-000000000030' },
      queuePosition: null,
      operationResult: 'CANCEL_REQUESTED',
      completed: false,
      error: null,
    });

    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({ where: expect.objectContaining({ leaseOwner: 'stale-worker' }) }),
    );
    expect(submissionUpdate).not.toHaveBeenCalled();
  });

  /** 验证过晚取消保留原 authority target，却把取消动作投影为 FAILED/cancel_too_late。 */
  it('maps an accepted cancel against a terminal target to cancel_too_late', async () => {
    const update = vi.fn().mockResolvedValue({});
    const client = {
      deliver: vi.fn().mockResolvedValue({
        commandId: '00000000-0000-4000-8000-000000000030',
        submissionId: '00000000-0000-4000-8000-000000000010',
        status: 'SUCCEEDED',
        target: { resourceType: 'COMMAND', resourceId: '00000000-0000-4000-8000-000000000030' },
        targetStatus: 'SUCCEEDED',
        childRunIds: [],
        queuePosition: null,
        acceptedAt: '2026-07-29T00:00:00.000Z',
      }),
    } as unknown as DataOperationsClient;
    const dispatcher = dispatcherWith(
      {
        client: {
          $transaction: vi.fn(async (callback) =>
            callback({
              apiOutbox: { updateMany: vi.fn().mockResolvedValue({ count: 1 }) },
              dataOperationSubmission: { update },
              auditLog: { create: vi.fn() },
            }),
          ),
        },
      },
      client,
    );

    await privateDeliverOne(dispatcher, cancelOutbox(), 'worker-a');

    expect(client.deliver).toHaveBeenCalledWith(
      '/internal/v1/data-operations/commands/cancel',
      expect.anything(),
      'dataops:00000000-0000-4000-8000-000000000010',
      'request-cancel-1',
    );
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          deliveryStatus: DataOperationDeliveryStatus.ACCEPTED,
          operationResult: 'FAILED',
          authorityType: 'COMMAND',
          authorityId: '00000000-0000-4000-8000-000000000030',
          safeError: expect.objectContaining({ code: 'cancel_too_late' }),
        }),
      }),
    );
  });

  /** 验证 replay 必须复验 ACTIVE SUPER_ADMIN、确认值并只重置原 outbox。 */
  it('replays only the original dead letter after explicit active-super-admin confirmation', async () => {
    const outbox: Record<string, unknown> = {
      ...cancelOutbox(),
      state: ApiOutboxState.DEAD_LETTER,
      attemptCount: 20,
    };
    const update = vi.fn().mockResolvedValue({});
    const submissionUpdate = vi.fn().mockResolvedValue({});
    const auditCreate = vi.fn().mockResolvedValue({});
    const dispatcher = dispatcherWith({
      client: {
        $transaction: vi.fn(async (callback) =>
          callback({
            user: {
              findUnique: vi.fn().mockResolvedValue({
                id: replayActor.userId,
                role: Role.SUPER_ADMIN,
                status: UserStatus.ACTIVE,
              }),
            },
            apiOutbox: { findUnique: vi.fn().mockResolvedValue(outbox), update },
            dataOperationSubmission: { update: submissionUpdate },
            auditLog: { create: auditCreate },
          }),
        ),
      },
    });

    await expect(
      dispatcher.replayDeadLetter(
        replayActor,
        outbox.submissionId as string,
        DEAD_LETTER_REPLAY_CONFIRMATION,
      ),
    ).resolves.toBeUndefined();
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({
        where: { id: outbox.id },
        data: expect.objectContaining({
          state: ApiOutboxState.PENDING,
          attemptCount: 0,
          leaseOwner: null,
          leaseUntil: null,
        }),
      }),
    );
    expect(submissionUpdate).toHaveBeenCalledWith(
      expect.objectContaining({
        where: { id: outbox.submissionId },
        data: expect.objectContaining({
          deliveryStatus: DataOperationDeliveryStatus.PENDING,
        }),
      }),
    );
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          action: 'dataops.delivery.replayed',
          metadata: expect.objectContaining({
            submissionId: outbox.submissionId,
            replayedBy: replayActor.userId,
            downstreamKeyHmac: expect.any(String),
          }),
        }),
      }),
    );
  });

  /** 验证没有精确确认值时不访问数据库，也不能把 replay 当普通重试使用。 */
  it('rejects replay without the explicit confirmation value', async () => {
    const transaction = vi.fn();
    const dispatcher = dispatcherWith({ client: { $transaction: transaction } });

    await expect(
      dispatcher.replayDeadLetter(replayActor, '00000000-0000-4000-8000-000000000010', 'NO'),
    ).rejects.toMatchObject({ status: 400 });
    expect(transaction).not.toHaveBeenCalled();
  });
});

/** 构造最小取消 outbox，内部幂等键固定由 SubmissionId 派生。 */
function cancelOutbox(): Record<string, unknown> {
  return {
    id: '00000000-0000-4000-8000-000000000020',
    submissionId: '00000000-0000-4000-8000-000000000010',
    downstreamIdempotencyKey: 'dataops:00000000-0000-4000-8000-000000000010',
    internalPath: '/internal/v1/data-operations/commands/cancel',
    canonicalPayload: {
      submissionId: '00000000-0000-4000-8000-000000000010',
      target: { resourceType: 'COMMAND', resourceId: '00000000-0000-4000-8000-000000000030' },
    },
    state: ApiOutboxState.DELIVERING,
    attemptCount: 1,
    nextAttemptAt: new Date('2026-07-29T00:00:00.000Z'),
    leaseOwner: 'worker-a',
    leaseUntil: new Date('2026-07-29T00:00:30.000Z'),
    lastProblemCode: null,
    lastAttemptAt: new Date('2026-07-29T00:00:00.000Z'),
    deliveredAt: null,
    createdAt: new Date('2026-07-29T00:00:00.000Z'),
    updatedAt: new Date('2026-07-29T00:00:00.000Z'),
    submission: {
      id: '00000000-0000-4000-8000-000000000010',
      actorId: '00000000-0000-4000-8000-000000000001',
      action: 'SYNC_CANCEL',
      requestId: 'request-cancel-1',
    },
  };
}

/** 将最小依赖注入 dispatcher，测试不连接真实 PostgreSQL 或 data-sync。 */
function dispatcherWith(
  database: unknown,
  client = {} as DataOperationsClient,
): DataOperationOutboxDispatcher {
  return new DataOperationOutboxDispatcher(database as DatabaseService, client, config);
}

/** 调用私有 accepted 写回分支，以验证租约比较在持久化前生效。 */
async function privateMarkAccepted(
  dispatcher: DataOperationOutboxDispatcher,
  outbox: Record<string, unknown>,
  owner: string,
  accepted: Record<string, unknown>,
): Promise<void> {
  const internal = dispatcher as unknown as {
    markAccepted: (outbox: unknown, owner: string, accepted: unknown) => Promise<void>;
  };
  await internal.markAccepted(outbox, owner, accepted);
}

/** 调用私有单项投递分支，以覆盖 action 专属权威回执解析。 */
async function privateDeliverOne(
  dispatcher: DataOperationOutboxDispatcher,
  outbox: Record<string, unknown>,
  owner: string,
): Promise<void> {
  const internal = dispatcher as unknown as {
    deliverOne: (outbox: unknown, owner: string) => Promise<void>;
  };
  await internal.deliverOne(outbox, owner);
}
