/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest asymmetric matchers intentionally return `any`. */

import { Role, UserStatus } from '../../../generated/prisma/client.js';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { AppConfigService } from '../../../config/app-config.service.js';
import * as argon2 from 'argon2';
import { describe, expect, it, vi } from 'vitest';

import { normalizeAccount, UserService } from '../user.service.js';

const configuration = {
  jwtAccessSecret: 'test-only-signing-secret-which-is-long-enough-for-hmac',
} as AppConfigService;

const actor: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000010',
  role: Role.ADMIN,
  securityVersion: 1,
};

// 汇集 UserService 的账号身份、目标策略与安全版本回归测试。
describe('UserService', () => {
  // 验证登录规范化接受任意输入形状，而创建流程负责执行格式约束。
  it('trims and lowercases login account identifiers', () => {
    expect(normalizeAccount('  MARKET.User ')).toBe('market.user');
  });

  // 验证多取一行的分页仅保留请求记录，并生成签名后的下一页游标。
  it('uses final visible item as next cursor without dropping extra record', async () => {
    const findMany = vi
      .fn()
      .mockResolvedValue([
        user('00000000-0000-4000-8000-000000000011'),
        user('00000000-0000-4000-8000-000000000012'),
        user('00000000-0000-4000-8000-000000000013'),
      ]);
    const service = new UserService({ client: { user: { findMany } } } as never, configuration);

    const result = await service.listUsers(actor, {
      pageSize: 2,
      sort: 'createdAt',
      order: 'desc',
    });

    expect(result.items.map((item) => item.id)).toEqual([
      '00000000-0000-4000-8000-000000000011',
      '00000000-0000-4000-8000-000000000012',
    ]);
    expect(result.page.nextCursor).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(findMany).toHaveBeenCalledWith(
      expect.objectContaining({ orderBy: [{ createdAt: 'desc' }, { id: 'desc' }], take: 3 }),
    );
  });

  // 验证列表范围只加入当前管理者身份，不暴露其他 SUPER_ADMIN 记录。
  it('includes the current manager as a read-only list identity beside manageable roles', async () => {
    const findMany = vi.fn().mockResolvedValue([]);
    const service = new UserService({ client: { user: { findMany } } } as never, configuration);
    const superAdminActor = { ...actor, role: Role.SUPER_ADMIN };

    await service.listUsers(superAdminActor, {
      pageSize: 20,
      sort: 'createdAt',
      order: 'desc',
    });

    expect(findMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: {
          AND: [
            {
              OR: [{ role: { in: [Role.USER, Role.ADMIN] } }, { id: superAdminActor.userId }],
            },
            { status: { not: UserStatus.DELETED } },
          ],
        },
      }),
    );
  });

  // 验证 ADMIN 不能通过普通用户管理输入创建 SUPER_ADMIN。
  it('rejects every ordinary SUPER_ADMIN creation path', async () => {
    const service = new UserService({ client: {} } as never, configuration);

    await expect(
      service.createUser(
        { ...actor, role: Role.SUPER_ADMIN },
        {
          account: 'market.admin',
          displayName: 'Market Admin',
          password: 'safe-password-2026',
          role: Role.SUPER_ADMIN,
        },
        { actorId: actor.userId },
      ),
    ).rejects.toMatchObject({ status: 403 });
  });

  // 验证状态或角色变化递增 securityVersion、撤销会话并追加审计证据。
  it('invalidates target sessions for a disabling administrator update', async () => {
    const currentActor = user(actor.userId, Role.ADMIN);
    const target = user('00000000-0000-4000-8000-000000000020', Role.USER);
    const updated = { ...target, status: UserStatus.DISABLED, version: 2, securityVersion: 2 };
    const updateMany = vi.fn().mockResolvedValue({ count: 1 });
    const sessionUpdateMany = vi.fn().mockResolvedValue({ count: 1 });
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      user: {
        findUnique: vi.fn().mockResolvedValueOnce(currentActor).mockResolvedValueOnce(target),
        updateMany,
        findUniqueOrThrow: vi.fn().mockResolvedValue(updated),
      },
      session: { updateMany: sessionUpdateMany },
      auditLog: { create: auditCreate },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await service.updateUser(actor, target.id, { status: UserStatus.DISABLED }, 1, {
      actorId: actor.userId,
    });

    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          securityVersion: { increment: 1 },
          version: { increment: 1 },
        }),
      }),
    );
    expect(sessionUpdateMany).toHaveBeenCalledWith(
      expect.objectContaining({ where: { userId: target.id, revokedAt: null } }),
    );
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({ data: expect.objectContaining({ action: 'user.admin.updated' }) }),
    );
  });

  // 验证 ETag 不匹配时在目标变更写入前稳定返回 412。
  it('rejects stale target ETag with precondition-failed', async () => {
    const currentActor = user(actor.userId, Role.ADMIN);
    const target = user('00000000-0000-4000-8000-000000000021', Role.USER);
    const updateMany = vi.fn();
    const transaction = {
      user: {
        findUnique: vi.fn().mockResolvedValueOnce(currentActor).mockResolvedValueOnce(target),
        updateMany,
      },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await expect(
      service.updateUser(actor, target.id, { displayName: 'Changed' }, 99, {
        actorId: actor.userId,
      }),
    ).rejects.toMatchObject({ status: 412 });
    expect(updateMany).not.toHaveBeenCalled();
  });

  // 验证 ADMIN 不能通过后端调用查看或管理另一个 ADMIN。
  it('hides administrator targets from ADMIN actor scope', async () => {
    const service = new UserService(
      {
        client: {
          user: {
            findUnique: vi
              .fn()
              .mockResolvedValue(user('00000000-0000-4000-8000-000000000022', Role.ADMIN)),
          },
        },
      } as never,
      configuration,
    );

    await expect(
      service.getUser(actor, '00000000-0000-4000-8000-000000000022'),
    ).rejects.toMatchObject({
      status: 404,
    });
  });

  // 验证 DELETE 同时递增资源与安全版本、撤销会话并追加审计证据。
  it('soft-deletes a managed user with security invalidation and audit transaction', async () => {
    const currentActor = user(actor.userId, Role.ADMIN);
    const target = user('00000000-0000-4000-8000-000000000023', Role.USER);
    const updateMany = vi.fn().mockResolvedValue({ count: 1 });
    const sessionUpdateMany = vi.fn().mockResolvedValue({ count: 1 });
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      user: {
        findUnique: vi.fn().mockResolvedValueOnce(currentActor).mockResolvedValueOnce(target),
        updateMany,
      },
      session: { updateMany: sessionUpdateMany },
      auditLog: { create: auditCreate },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await service.deleteUser(actor, target.id, 1, { actorId: actor.userId });

    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          status: UserStatus.DELETED,
          securityVersion: { increment: 1 },
          version: { increment: 1 },
        }),
      }),
    );
    expect(sessionUpdateMany).toHaveBeenCalledOnce();
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({ data: expect.objectContaining({ action: 'user.deleted' }) }),
    );
  });

  // 验证重置密码会使会话失效，且审计元数据不保存请求明文。
  it('resets a managed password with security invalidation and no plaintext audit field', async () => {
    const currentActor = user(actor.userId, Role.ADMIN);
    const target = user('00000000-0000-4000-8000-000000000024', Role.USER);
    const updateMany = vi.fn().mockResolvedValue({ count: 1 });
    const credentialUpdate = vi.fn().mockResolvedValue({});
    const sessionUpdateMany = vi.fn().mockResolvedValue({ count: 1 });
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      user: {
        findUnique: vi.fn().mockResolvedValueOnce(currentActor).mockResolvedValueOnce(target),
        updateMany,
      },
      credential: { update: credentialUpdate },
      session: { updateMany: sessionUpdateMany },
      auditLog: { create: auditCreate },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await service.resetPassword(actor, target.id, { password: 'new-password-2026' }, 1, {
      actorId: actor.userId,
    });

    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        data: { securityVersion: { increment: 1 }, version: { increment: 1 } },
      }),
    );
    expect(credentialUpdate).toHaveBeenCalledOnce();
    expect(sessionUpdateMany).toHaveBeenCalledOnce();
    const auditCalls = auditCreate.mock.calls as unknown as Array<Array<{ data?: unknown }>>;
    const auditData = auditCalls[0]?.[0]?.data;
    expect(JSON.stringify(auditData)).not.toContain('new-password-2026');
  });

  // 验证 SUPER_ADMIN 将 USER 提升角色时，全部既有授权会话同时失效。
  it('invalidates target sessions for a role change', async () => {
    const superActor = { ...actor, role: Role.SUPER_ADMIN };
    const currentActor = user(superActor.userId, Role.SUPER_ADMIN);
    const target = user('00000000-0000-4000-8000-000000000025', Role.USER);
    const updateMany = vi.fn().mockResolvedValue({ count: 1 });
    const sessionUpdateMany = vi.fn().mockResolvedValue({ count: 1 });
    const transaction = {
      user: {
        findUnique: vi.fn().mockResolvedValueOnce(currentActor).mockResolvedValueOnce(target),
        updateMany,
        findUniqueOrThrow: vi
          .fn()
          .mockResolvedValue({ ...target, role: Role.ADMIN, version: 2, securityVersion: 2 }),
      },
      session: { updateMany: sessionUpdateMany },
      auditLog: { create: vi.fn().mockResolvedValue({}) },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await service.updateUser(superActor, target.id, { role: Role.ADMIN }, 1, {
      actorId: superActor.userId,
    });

    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({ securityVersion: { increment: 1 } }),
      }),
    );
    expect(sessionUpdateMany).toHaveBeenCalledOnce();
  });

  // 验证自助改密递增 securityVersion，并撤销变更前的全部会话。
  it('changes own password with security invalidation and audit transaction', async () => {
    const currentActor = user(actor.userId, Role.ADMIN);
    const credential = { passwordHash: await argon2.hash('old-password-2026') };
    const userUpdate = vi.fn().mockResolvedValue({});
    const sessionUpdateMany = vi.fn().mockResolvedValue({ count: 1 });
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      user: { findUnique: vi.fn().mockResolvedValue(currentActor), update: userUpdate },
      credential: {
        findUnique: vi.fn().mockResolvedValue(credential),
        update: vi.fn().mockResolvedValue({}),
      },
      session: { updateMany: sessionUpdateMany },
      auditLog: { create: auditCreate },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await service.changePassword(
      actor,
      { currentPassword: 'old-password-2026', newPassword: 'new-password-2026' },
      {
        actorId: actor.userId,
      },
    );

    expect(userUpdate).toHaveBeenCalledWith(
      expect.objectContaining({
        data: { securityVersion: { increment: 1 }, version: { increment: 1 } },
      }),
    );
    expect(sessionUpdateMany).toHaveBeenCalledOnce();
    expect(auditCreate).toHaveBeenCalledOnce();
  });

  // 验证当前密码错误使用可区分的稳定 Problem code，供 Web 避免误触发 token refresh。
  it('returns current-password-invalid for a wrong current password', async () => {
    const currentActor = user(actor.userId, Role.ADMIN);
    const transaction = {
      user: { findUnique: vi.fn().mockResolvedValue(currentActor) },
      credential: {
        findUnique: vi.fn().mockResolvedValue({
          passwordHash: await argon2.hash('actual-password-2026'),
        }),
      },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await expect(
      service.changePassword(
        actor,
        { currentPassword: 'wrong-password-2026', newPassword: 'new-password-2026' },
        { actorId: actor.userId },
      ),
    ).rejects.toMatchObject({
      status: 401,
      response: { code: 'current-password-invalid' },
    });
  });

  // 验证只有显式选定的 ACTIVE ADMIN 能执行一次性 SUPER_ADMIN 运维提升。
  it('promotes an existing active administrator once with session invalidation and audit evidence', async () => {
    const target = user('00000000-0000-4000-8000-000000000026', Role.ADMIN);
    const updateMany = vi.fn().mockResolvedValue({ count: 1 });
    const sessionUpdateMany = vi.fn().mockResolvedValue({ count: 1 });
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = {
      $executeRaw: vi.fn().mockResolvedValue(1),
      user: {
        findFirst: vi.fn().mockResolvedValue(null),
        findUnique: vi.fn().mockResolvedValue(target),
        updateMany,
        findUniqueOrThrow: vi
          .fn()
          .mockResolvedValue({ ...target, role: Role.SUPER_ADMIN, version: 2, securityVersion: 2 }),
      },
      session: { updateMany: sessionUpdateMany },
      auditLog: { create: auditCreate },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    const promoted = await service.promoteExistingAdminToSuperAdmin(target.account);

    expect(promoted.role).toBe(Role.SUPER_ADMIN);
    expect(updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: { id: target.id, role: Role.ADMIN, status: UserStatus.ACTIVE },
        data: expect.objectContaining({ securityVersion: { increment: 1 } }),
      }),
    );
    expect(sessionUpdateMany).toHaveBeenCalledWith(
      expect.objectContaining({ where: { userId: target.id, revokedAt: null } }),
    );
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({ action: 'system.bootstrap.super_admin_promoted' }),
      }),
    );
  });

  // 验证即使提供另一个合法 ADMIN 账号，提升操作仍只能执行一次。
  it('refuses operational promotion when a super administrator already exists', async () => {
    const findFirst = vi.fn().mockResolvedValue({ id: '00000000-0000-4000-8000-000000000027' });
    const transaction = {
      $executeRaw: vi.fn().mockResolvedValue(1),
      user: { findFirst, findUnique: vi.fn(), updateMany: vi.fn() },
    };
    const service = new UserService(
      {
        client: {
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as never,
      configuration,
    );

    await expect(service.promoteExistingAdminToSuperAdmin('market.admin')).rejects.toMatchObject({
      status: 409,
    });
    expect(transaction.user.findUnique).not.toHaveBeenCalled();
    expect(transaction.user.updateMany).not.toHaveBeenCalled();
  });
});

/** 构造不含凭据材料的持久化用户 fixture，供 UserService 策略测试使用。 */
function user(id: string, role: Role = Role.USER) {
  const now = new Date('2026-07-26T00:00:00.000Z');
  return {
    id,
    account: `user.${id.slice(-5)}`,
    normalizedAccount: `user.${id.slice(-5)}`,
    displayName: id,
    role,
    status: UserStatus.ACTIVE,
    securityVersion: 1,
    version: 1,
    lastLoginAt: null,
    deletedAt: null,
    createdAt: now,
    updatedAt: now,
  };
}
