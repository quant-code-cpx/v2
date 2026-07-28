/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest asymmetric matchers intentionally return `any`. */

import { Role } from '../../../generated/prisma/client.js';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { AppConfigService } from '../../../config/app-config.service.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import { describe, expect, it, vi } from 'vitest';

import { AuthService } from '../auth.service.js';

const actor: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000010',
  role: Role.USER,
  securityVersion: 1,
};
const configuration = {
  jwtAccessSecret: 'test-only-session-cursor-secret-long-enough',
} as AppConfigService;

// 汇集本人会话族读取、资源授权和事务撤销语义测试。
describe('AuthService session family management', () => {
  // 验证列表仅返回本人活动行，并正确标识当前 family。
  it('lists active families with current-session semantics', async () => {
    const createdAt = new Date('2026-07-28T08:00:00.000Z');
    const absoluteExpiresAt = new Date('2026-08-28T08:00:00.000Z');
    const client = {
      session: {
        findUnique: vi.fn().mockResolvedValue({ familyId: actor.sessionId }),
        findMany: vi.fn().mockResolvedValue([
          {
            id: actor.sessionId,
            familyId: actor.sessionId,
            createdAt,
            absoluteExpiresAt,
          },
        ]),
        count: vi.fn().mockResolvedValue(1),
      },
      // 读取列表使用同一数据库快照组合当前会话、页面和总数。
      $transaction: (operations: Array<Promise<unknown>>) => Promise.all(operations),
    };
    const service = authWithClient(client);

    const result = await service.listMySessionFamilies(actor, { pageSize: 20 });

    expect(result).toEqual({
      items: [
        {
          familyId: actor.sessionId,
          current: true,
          lastActiveAt: createdAt.toISOString(),
          absoluteExpiresAt: absoluteExpiresAt.toISOString(),
        },
      ],
      page: { nextCursor: null },
      total: 1,
    });
    expect(client.session.findMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: expect.objectContaining({ userId: actor.userId, revokedAt: null }),
      }),
    );
  });

  // 验证未知或其他用户 family 统一返回 404，且不会尝试写状态或审计。
  it('hides foreign session family ownership behind not found', async () => {
    const updateMany = vi.fn();
    const auditCreate = vi.fn();
    const transaction = {
      session: {
        findFirst: vi.fn().mockResolvedValue(null),
        updateMany,
      },
      auditLog: { create: auditCreate },
    };
    const service = authWithClient({
      // 写用例在同一事务替身中执行资源授权与状态更新。
      $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
        callback(transaction),
    });

    await expect(
      service.revokeMySessionFamily(actor, '00000000-0000-4000-8000-000000000099', 'request-1'),
    ).rejects.toMatchObject({ status: 404 });
    expect(updateMany).not.toHaveBeenCalled();
    expect(auditCreate).not.toHaveBeenCalled();
  });

  // 验证当前 family 撤销与审计共同提交，并向 Controller 返回当前会话语义。
  it('revokes the current family and appends audit evidence atomically', async () => {
    const updateMany = vi.fn().mockResolvedValue({ count: 1 });
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      session: {
        findFirst: vi.fn().mockResolvedValue({ familyId: actor.sessionId }),
        updateMany,
        findUnique: vi.fn().mockResolvedValue({ familyId: actor.sessionId }),
      },
      auditLog: { create: auditCreate },
    };
    const service = authWithClient({
      // 写用例在同一事务替身中执行资源授权、撤销和审计。
      $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
        callback(transaction),
    });

    await expect(service.revokeMySessionFamily(actor, actor.sessionId, 'request-1')).resolves.toBe(
      true,
    );
    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: expect.objectContaining({ userId: actor.userId, familyId: actor.sessionId }),
      }),
    );
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({ action: 'auth.session_family.revoked' }),
      }),
    );
  });

  // 验证撤销其他会话只按真正更新的 distinct family 计数，并保留当前 family。
  it('revokes other active families and returns the changed family count', async () => {
    const otherFamily = '00000000-0000-4000-8000-000000000020';
    const anotherFamily = '00000000-0000-4000-8000-000000000021';
    const updateManyAndReturn = vi
      .fn()
      .mockResolvedValue([
        { familyId: otherFamily },
        { familyId: otherFamily },
        { familyId: anotherFamily },
      ]);
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      session: {
        findUnique: vi.fn().mockResolvedValue({ familyId: actor.sessionId }),
        updateManyAndReturn,
      },
      auditLog: { create: auditCreate },
    };
    const service = authWithClient({
      // 写用例在同一事务替身中读取当前 family 并撤销其他 family。
      $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
        callback(transaction),
    });

    await expect(service.revokeMyOtherSessionFamilies(actor, 'request-1')).resolves.toEqual({
      revokedFamilyCount: 2,
    });
    expect(updateManyAndReturn).toHaveBeenCalledWith(
      expect.objectContaining({
        where: expect.objectContaining({
          userId: actor.userId,
          familyId: { not: actor.sessionId },
          revokedAt: null,
        }),
        select: { familyId: true },
      }),
    );
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          action: 'auth.session_families.others_revoked',
          metadata: { revokedFamilyCount: 2 },
        }),
      }),
    );
  });

  // 验证没有实际状态变化时保持幂等成功，且不制造重复审计记录。
  it('keeps revoke-others idempotent when no active family changes', async () => {
    const auditCreate = vi.fn();
    const transaction = {
      session: {
        findUnique: vi.fn().mockResolvedValue({ familyId: actor.sessionId }),
        updateManyAndReturn: vi.fn().mockResolvedValue([]),
      },
      auditLog: { create: auditCreate },
    };
    const service = authWithClient({
      // 写用例在同一事务替身中返回无变化终态。
      $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
        callback(transaction),
    });

    await expect(service.revokeMyOtherSessionFamilies(actor, 'request-1')).resolves.toEqual({
      revokedFamilyCount: 0,
    });
    expect(auditCreate).not.toHaveBeenCalled();
  });
});

/** 构造只注入 Session 用例所需协作者的 AuthService。 */
function authWithClient(client: object): AuthService {
  return new AuthService(
    { client } as unknown as DatabaseService,
    {} as never,
    {} as never,
    {} as never,
    configuration,
    {} as never,
  );
}
