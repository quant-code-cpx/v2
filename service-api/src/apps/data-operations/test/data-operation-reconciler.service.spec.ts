/* eslint-disable @typescript-eslint/no-unsafe-assignment, @typescript-eslint/unbound-method */

import type { DataOperationsClient } from '../../../data-sync/clients/data-operations.client.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import { describe, expect, it, vi } from 'vitest';

import { DataOperationReconcilerService } from '../data-operation-reconciler.service.js';

/** 覆盖 health batch 权威详情对账，避免仅依赖批次状态而遗漏 target 结果。 */
describe('DataOperationReconcilerService', () => {
  /** 验证健康检查只有全部有序 target 终态且成功项带 evaluationId 时才完成 Submission。 */
  it('reconciles health check detail only after every target is traceable', async () => {
    const update = vi.fn().mockResolvedValue({});
    const client = {
      getHealthCheck: vi.fn().mockResolvedValue({
        healthCheckId: '00000000-0000-4000-8000-000000000030',
        status: 'SUCCEEDED',
        targets: [
          {
            target: { datasetCode: 'equity.daily', dataVersion: null },
            resolvedDataVersion: '00000000-0000-4000-8000-000000000040',
            status: 'SUCCEEDED',
            evaluationId: '00000000-0000-4000-8000-000000000050',
            error: null,
          },
          {
            target: { datasetCode: 'index.members', dataVersion: null },
            resolvedDataVersion: '00000000-0000-4000-8000-000000000060',
            status: 'SUCCEEDED',
            evaluationId: '00000000-0000-4000-8000-000000000070',
            error: null,
          },
        ],
      }),
    } as unknown as DataOperationsClient;
    const service = reconcilerWith(
      {
        client: {
          dataOperationSubmission: {
            findMany: vi.fn().mockResolvedValue([healthSubmission()]),
            update,
          },
        },
      },
      client,
    );

    await expect(service.reconcileOnce()).resolves.toBe(1);
    expect(client.getHealthCheck).toHaveBeenCalledWith(
      { healthCheckId: '00000000-0000-4000-8000-000000000030' },
      'request-health-1',
    );
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          operationResult: 'SUCCEEDED',
          completedAt: expect.any(Date),
        }),
      }),
    );
  });

  /** 验证批次状态即使误报终态，只要有 target 尚在运行就不会写入不完整投影。 */
  it('keeps health submission unfinished while one target is non-terminal', async () => {
    const update = vi.fn();
    const client = {
      getHealthCheck: vi.fn().mockResolvedValue({
        status: 'SUCCEEDED',
        targets: [
          {
            target: { datasetCode: 'equity.daily', dataVersion: null },
            resolvedDataVersion: null,
            status: 'RUNNING',
            evaluationId: null,
            error: null,
          },
        ],
      }),
    } as unknown as DataOperationsClient;
    const service = reconcilerWith(
      {
        client: {
          dataOperationSubmission: {
            findMany: vi.fn().mockResolvedValue([healthSubmission()]),
            update,
          },
        },
      },
      client,
    );

    await expect(service.reconcileOnce()).resolves.toBe(0);
    expect(update).not.toHaveBeenCalled();
  });

  /** 验证 INTERRUPTED 仍会继续对账，且取消动作不会被错误标记为 cancel_too_late。 */
  it('treats interrupted cancel targets as recoverable rather than terminal', async () => {
    const update = vi.fn().mockResolvedValue({});
    const client = {
      getCommand: vi.fn().mockResolvedValue({ status: 'INTERRUPTED' }),
    } as unknown as DataOperationsClient;
    const service = reconcilerWith(
      {
        client: {
          dataOperationSubmission: {
            findMany: vi.fn().mockResolvedValue([
              {
                ...healthSubmission(),
                action: 'SYNC_CANCEL',
                authorityType: 'COMMAND',
                operationResult: 'CANCEL_REQUESTED',
                requestId: 'request-cancel-interrupted',
              },
            ]),
            update,
          },
        },
      },
      client,
    );

    await expect(service.reconcileOnce()).resolves.toBe(1);
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          operationResult: 'INTERRUPTED',
          completedAt: null,
        }),
      }),
    );
  });

  /** 验证已完成 Submission 不会占用有限对账批次，避免较早历史记录饿死非终态操作。 */
  it('queries only unfinished accepted submissions', async () => {
    const findMany = vi.fn().mockResolvedValue([]);
    const service = reconcilerWith(
      {
        client: {
          dataOperationSubmission: {
            findMany,
            update: vi.fn(),
          },
        },
      },
      {} as DataOperationsClient,
    );

    await expect(service.reconcileOnce(20)).resolves.toBe(0);
    expect(findMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: expect.objectContaining({
          deliveryStatus: 'ACCEPTED',
          completedAt: null,
        }),
      }),
    );
  });

  /** 验证失败 target 缺少合同要求的脱敏错误时，对账器保持 Submission 未完成。 */
  it('keeps health submission unfinished when a failed target omits its error', async () => {
    const update = vi.fn();
    const client = {
      getHealthCheck: vi.fn().mockResolvedValue({
        status: 'FAILED',
        targets: [
          {
            target: { datasetCode: 'equity.daily', dataVersion: null },
            resolvedDataVersion: null,
            status: 'FAILED',
            evaluationId: null,
            error: null,
          },
        ],
      }),
    } as unknown as DataOperationsClient;
    const service = reconcilerWith(
      {
        client: {
          dataOperationSubmission: {
            findMany: vi.fn().mockResolvedValue([healthSubmission()]),
            update,
          },
        },
      },
      client,
    );

    await expect(service.reconcileOnce()).resolves.toBe(0);
    expect(update).not.toHaveBeenCalled();
  });
});

/** 构造一个已被 data-sync 接受、等待 HEALTH_CHECK 权威对账的最小 Submission。 */
function healthSubmission(): Record<string, unknown> {
  return {
    id: '00000000-0000-4000-8000-000000000010',
    action: 'HEALTH_CHECK_SUBMIT',
    deliveryStatus: 'ACCEPTED',
    authorityType: 'HEALTH_CHECK',
    authorityId: '00000000-0000-4000-8000-000000000030',
    operationResult: 'QUEUED',
    completedAt: null,
    safeError: null,
    requestId: 'request-health-1',
  };
}

/** 注入仅包含对账查询和更新方法的最小依赖。 */
function reconcilerWith(
  database: unknown,
  client: DataOperationsClient,
): DataOperationReconcilerService {
  return new DataOperationReconcilerService(database as DatabaseService, client);
}
