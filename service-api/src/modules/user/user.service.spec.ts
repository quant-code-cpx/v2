/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest asymmetric matchers intentionally return `any`. */

import { Role, UserStatus } from '../../generated/prisma/client.js';
import type { AuthContext } from '../../platform/http/auth-context.js';
import type { AppConfigService } from '../../platform/config/app-config.service.js';
import * as argon2 from 'argon2';
import { describe, expect, it, vi } from 'vitest';

import { normalizeAccount, UserService } from './user.service.js';

const configuration = {
  jwtAccessSecret: 'test-only-signing-secret-which-is-long-enough-for-hmac',
} as AppConfigService;

const actor: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000010',
  role: Role.ADMIN,
  securityVersion: 1,
};

// Group account identity, target policy, and security-version regression tests for UserService.
describe('UserService', () => {
  // Verify login normalization accepts arbitrary input shape while creation owns format enforcement.
  it('trims and lowercases login account identifiers', () => {
    expect(normalizeAccount('  MARKET.User ')).toBe('market.user');
  });

  // Verify extra-row pagination retains only requested records and derives a signed next cursor.
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

  // Verify list scope adds only the current manager identity, never another SUPER_ADMIN record.
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

  // Verify ADMIN cannot create a SUPER_ADMIN through ordinary user-management input.
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

  // Verify status or role change advances securityVersion, revokes sessions, and appends audit evidence.
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

  // Verify ETag mismatch produces stable 412 before a target mutation writes anything.
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

  // Verify ADMIN cannot use backend calls to view or manage an ADMIN target.
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

  // Verify DELETE advances both resource and security versions, revokes sessions, and appends audit evidence.
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

  // Verify password reset invalidates sessions and never puts plaintext request input into audit metadata.
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

  // Verify SUPER_ADMIN role promotion of a USER also invalidates all previously authorized sessions.
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

  // Verify self-service password change increments securityVersion and revokes every pre-change session.
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

  // Verify only an explicitly selected active ADMIN can take the one-time operational SUPER_ADMIN path.
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

  // Verify promotion stays one-time even when the operator supplies another otherwise valid ADMIN account.
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

/** Build a persisted-user fixture containing no credential material for UserService policy tests. */
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
