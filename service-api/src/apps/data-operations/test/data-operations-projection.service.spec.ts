/* eslint-disable @typescript-eslint/no-unsafe-assignment, @typescript-eslint/require-await, @typescript-eslint/unbound-method */

import { Role } from '../../../generated/prisma/client.js';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { DataOperationsClient } from '../../../data-sync/clients/data-operations.client.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import { describe, expect, it, vi } from 'vitest';

import { DataOperationsProjectionService } from '../data-operations-projection.service.js';
import type { DataOperationSubmissionService } from '../data-operation-submission.service.js';

/** 提供有读取权限的测试管理员，用于绑定公开操作记录联合 cursor。 */
const reader: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000002',
  role: Role.ADMIN,
  securityVersion: 1,
};

/** 覆盖本地 Submission 与 data-sync SYSTEM 事件的无重复联合分页。 */
describe('DataOperationsProjectionService operations cursor', () => {
  /** 验证跨页合并保持时间倒序，不重复本地或系统项目且冻结第一次 occurredTo。 */
  it('merges local submissions and system events without duplicates across pages', async () => {
    let storedCursor: Record<string, unknown> | null = null;
    const create = vi
      .fn()
      .mockImplementation(async ({ data }: { data: Record<string, unknown> }) => {
        storedCursor = {
          id: '00000000-0000-4000-8000-000000000099',
          version: 1,
          ...data,
        };
        return storedCursor;
      });
    const localA = submission('00000000-0000-4000-8000-000000000010', '2026-07-29T10:00:00.000Z');
    const localB = submission('00000000-0000-4000-8000-000000000011', '2026-07-29T08:00:00.000Z');
    const submissions = {
      searchSubmissions: vi
        .fn()
        .mockResolvedValueOnce([localA, localB])
        .mockResolvedValueOnce([localB])
        .mockResolvedValueOnce([localB]),
      findByRequestIds: vi.fn().mockResolvedValue([]),
    } as unknown as DataOperationSubmissionService;
    const client = {
      searchEvents: vi.fn().mockResolvedValue({
        items: [
          {
            eventId: '00000000-0000-4000-8000-000000000020',
            resourceType: 'SCHEDULE',
            resourceId: '00000000-0000-4000-8000-000000000021',
            action: 'SCHEDULE_TICK',
            result: 'SUCCEEDED',
            actorRef: 'system:schedule/equity.daily',
            requestId: 'system-request-1',
            occurredAt: '2026-07-29T09:00:00.000Z',
            error: null,
          },
        ],
        nextCursor: null,
      }),
    } as unknown as DataOperationsClient;
    const service = new DataOperationsProjectionService(
      {
        client: {
          dataOperationSearchCursor: {
            create,
            findUnique: vi.fn(async () => storedCursor),
            updateMany: vi.fn().mockResolvedValue({ count: 1 }),
          },
        },
      } as unknown as DatabaseService,
      submissions,
      client,
    );

    const first = await service.searchOperations(reader, { limit: 2 }, 'operations-request-1');
    const second = await service.searchOperations(
      reader,
      { limit: 2, cursor: first.nextCursor },
      'operations-request-2',
    );
    const ids = [...first.items, ...second.items].map(
      (item) => (item as { submissionId: string | null }).submissionId,
    );

    expect(first.nextCursor).toBe('00000000-0000-4000-8000-000000000099');
    expect(second.nextCursor).toBeNull();
    expect(ids).toEqual([
      '00000000-0000-4000-8000-000000000010',
      null,
      '00000000-0000-4000-8000-000000000011',
    ]);
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({ data: expect.objectContaining({ occurredTo: expect.any(Date) }) }),
    );
    expect(client.searchEvents).toHaveBeenCalledTimes(1);
  });

  /** 验证 actorIds 筛选不会泄漏无法映射为公开 User UUID 的 SYSTEM 事件。 */
  it('excludes system events when the request filters by actorIds', async () => {
    const submissions = {
      searchSubmissions: vi.fn().mockResolvedValue([]),
      findByRequestIds: vi.fn().mockResolvedValue([]),
    } as unknown as DataOperationSubmissionService;
    const client = {
      searchEvents: vi.fn().mockResolvedValue({
        items: [
          {
            eventId: '00000000-0000-4000-8000-000000000020',
            resourceType: 'SCHEDULE',
            resourceId: '00000000-0000-4000-8000-000000000021',
            action: 'SCHEDULE_TICK',
            result: 'SUCCEEDED',
            actorRef: 'system:schedule/equity.daily',
            requestId: 'system-request-1',
            occurredAt: '2026-07-29T09:00:00.000Z',
            error: null,
          },
        ],
        nextCursor: null,
      }),
    } as unknown as DataOperationsClient;
    const service = new DataOperationsProjectionService(
      {
        client: {
          dataOperationSearchCursor: {
            create: vi.fn(),
            findUnique: vi.fn(),
            updateMany: vi.fn(),
          },
        },
      } as unknown as DatabaseService,
      submissions,
      client,
    );

    await expect(
      service.searchOperations(
        reader,
        { actorIds: [reader.userId], limit: 10 },
        'operations-request-3',
      ),
    ).resolves.toEqual({ items: [], nextCursor: null });
  });
});

/** 构造可公开投影、带 ACTIVE 用户的最小本地 Submission。 */
function submission(id: string, authorizedAt: string): Record<string, unknown> {
  return {
    id,
    action: 'SYNC_SUBMIT',
    sanitizedRequest: { targets: [{ datasetCode: 'equity.daily' }] },
    reason: '补齐历史同步',
    deliveryStatus: 'ACCEPTED',
    operationResult: 'QUEUED',
    authorityType: 'COMMAND',
    authorityId: '00000000-0000-4000-8000-000000000030',
    authorizedAt: new Date(authorizedAt),
    completedAt: null,
    lastObservedAt: new Date(authorizedAt),
    requestId: `request-${id}`,
    safeError: null,
    actorRef: 'user:opaque-reference',
    actor: {
      id: reader.userId,
      displayName: '运维管理员',
      status: 'ACTIVE',
    },
  };
}
