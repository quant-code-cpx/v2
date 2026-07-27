/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest 非对称匹配器会返回 `any`。 */

import { Role, UserStatus } from '../../../generated/prisma/client.js';
import type { AppConfigService } from '../../../platform/config/app-config.service.js';
import { describe, expect, it, vi } from 'vitest';

import { UserService } from '../user.service.js';

const configuration = {
  jwtAccessSecret: 'test-only-signing-secret-which-is-long-enough-for-hmac',
} as AppConfigService;

/** 描述初始化流程所需的最小 Prisma 事务替身，隔离策略测试无需真实数据库。 */
type BootstrapTransaction = {
  $executeRaw: ReturnType<typeof vi.fn>;
  auditLog: { create: ReturnType<typeof vi.fn> };
  user: {
    count: ReturnType<typeof vi.fn>;
    create: ReturnType<typeof vi.fn>;
    findFirst: ReturnType<typeof vi.fn>;
  };
};

/** 表示公开资源序列化所需的持久化用户字段，故意不包含密码凭证。 */
type PersistedUser = {
  account: string;
  createdAt: Date;
  deletedAt: Date | null;
  displayName: string;
  id: string;
  lastLoginAt: Date | null;
  normalizedAccount: string;
  role: Role;
  securityVersion: number;
  status: UserStatus;
  updatedAt: Date;
  version: number;
};

/** 覆盖超级管理员初始化的安全边界与幂等行为。 */
describe('UserService.ensureBootstrapSuperAdmin', () => {
  /** 验证空用户库会在单一事务中创建账号、凭证和审计记录。 */
  it('creates one super administrator and audit evidence for an empty user store', async () => {
    const created = persistedUser('00000000-0000-4000-8000-000000000101', Role.SUPER_ADMIN);
    const transaction = createBootstrapTransaction({ created });
    const { service, transactionRunner } = createBootstrapService(transaction);

    const result = await service.ensureBootstrapSuperAdmin('apex.root', 'Apex-root-password-2026');

    expect(result).toMatchObject({
      created: true,
      user: { id: created.id, role: Role.SUPER_ADMIN },
    });
    expect(transaction.$executeRaw).toHaveBeenCalledOnce();
    expect(transaction.user.create).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({
          account: 'apex.root',
          normalizedAccount: 'apex.root',
          role: Role.SUPER_ADMIN,
          status: UserStatus.ACTIVE,
        }),
      }),
    );
    expect(transaction.auditLog.create).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({ action: 'system.bootstrap.super_admin_created' }),
      }),
    );
    const auditPayload = JSON.stringify(transaction.auditLog.create.mock.calls[0]);
    expect(auditPayload).not.toContain('Apex-root-password-2026');
    expect(transactionRunner).toHaveBeenCalledWith(
      expect.any(Function),
      expect.objectContaining({ isolationLevel: 'Serializable' }),
    );
  });

  /** 验证活动超级管理员存在时，无论初始化凭证是否配置都不会改密、覆盖或重复审计。 */
  it('returns a no-op for an existing active super administrator', async () => {
    const existing = persistedUser('00000000-0000-4000-8000-000000000102', Role.SUPER_ADMIN);
    const transaction = createBootstrapTransaction({ existingSuperAdmin: existing });
    const { service } = createBootstrapService(transaction);

    const result = await service.ensureBootstrapSuperAdmin(undefined, undefined);

    expect(result).toMatchObject({ created: false, user: { id: existing.id } });
    expect(transaction.user.count).not.toHaveBeenCalled();
    expect(transaction.user.create).not.toHaveBeenCalled();
    expect(transaction.auditLog.create).not.toHaveBeenCalled();
  });

  /** 验证禁用的超级管理员不会被启动任务替换。 */
  it('refuses to replace a disabled super administrator', async () => {
    const transaction = createBootstrapTransaction({
      existingSuperAdmin: {
        ...persistedUser('00000000-0000-4000-8000-000000000103', Role.SUPER_ADMIN),
        status: UserStatus.DISABLED,
      },
    });
    const { service } = createBootstrapService(transaction);

    await expect(
      service.ensureBootstrapSuperAdmin('apex.root', 'Apex-root-password-2026'),
    ).rejects.toMatchObject({ status: 409 });
    expect(transaction.user.create).not.toHaveBeenCalled();
    expect(transaction.auditLog.create).not.toHaveBeenCalled();
  });

  /** 验证软删除的超级管理员也只能通过显式恢复流程处理。 */
  it('refuses to replace a deleted super administrator', async () => {
    const transaction = createBootstrapTransaction({
      existingSuperAdmin: {
        ...persistedUser('00000000-0000-4000-8000-000000000104', Role.SUPER_ADMIN),
        status: UserStatus.DELETED,
        deletedAt: new Date('2026-07-26T00:00:00.000Z'),
      },
    });
    const { service } = createBootstrapService(transaction);

    await expect(
      service.ensureBootstrapSuperAdmin('apex.root', 'Apex-root-password-2026'),
    ).rejects.toMatchObject({ status: 409 });
    expect(transaction.user.create).not.toHaveBeenCalled();
    expect(transaction.auditLog.create).not.toHaveBeenCalled();
  });

  /** 验证无超级管理员但已有任意用户时会安全失败，不会将目标账号提权或覆盖。 */
  it('refuses bootstrap when a non-super-administrator account already occupies the store', async () => {
    const transaction = createBootstrapTransaction({ userCount: 1 });
    const { service } = createBootstrapService(transaction);

    await expect(
      service.ensureBootstrapSuperAdmin('apex.root', 'Apex-root-password-2026'),
    ).rejects.toMatchObject({ status: 409 });
    expect(transaction.user.create).not.toHaveBeenCalled();
    expect(transaction.auditLog.create).not.toHaveBeenCalled();
  });

  /** 验证空库缺少初始化凭证时失败，避免生成不可控默认账号。 */
  it('requires explicit bootstrap credentials when no super administrator exists', async () => {
    const transaction = createBootstrapTransaction();
    const { service } = createBootstrapService(transaction);

    await expect(service.ensureBootstrapSuperAdmin(undefined, undefined)).rejects.toMatchObject({
      status: 400,
    });
    expect(transaction.user.create).not.toHaveBeenCalled();
    expect(transaction.auditLog.create).not.toHaveBeenCalled();
  });
});

/** 构造可配置的事务替身，以便断言初始化路径没有隐藏的写入。 */
function createBootstrapTransaction(
  options: {
    created?: PersistedUser;
    existingSuperAdmin?: PersistedUser | null;
    userCount?: number;
  } = {},
): BootstrapTransaction {
  return {
    $executeRaw: vi.fn().mockResolvedValue(1),
    user: {
      findFirst: vi.fn().mockResolvedValue(options.existingSuperAdmin ?? null),
      count: vi.fn().mockResolvedValue(options.userCount ?? 0),
      create: vi
        .fn()
        .mockResolvedValue(
          options.created ??
            persistedUser('00000000-0000-4000-8000-000000000199', Role.SUPER_ADMIN),
        ),
    },
    auditLog: { create: vi.fn().mockResolvedValue({}) },
  };
}

/** 将事务替身注入 `UserService`，并保留事务调用参数供测试断言。 */
function createBootstrapService(transaction: BootstrapTransaction): {
  service: UserService;
  transactionRunner: ReturnType<typeof vi.fn>;
} {
  const transactionRunner = vi.fn(
    async (callback: (value: BootstrapTransaction) => Promise<unknown>) => callback(transaction),
  );
  const service = new UserService(
    { client: { $transaction: transactionRunner } } as never,
    configuration,
  );
  return { service, transactionRunner };
}

/** 生成满足公开资源序列化所需字段的用户记录，不包含任何凭证数据。 */
function persistedUser(id: string, role: Role): PersistedUser {
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
