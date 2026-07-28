/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest asymmetric matchers intentionally return `any`. */

import { Role } from '../../../generated/prisma/client.js';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { AppConfigService } from '../../../config/app-config.service.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import { describe, expect, it, vi } from 'vitest';

import { AuditService } from '../audit.service.js';

const superActor: AuthContext = {
  userId: '00000000-0000-4000-8000-000000000001',
  sessionId: '00000000-0000-4000-8000-000000000010',
  role: Role.SUPER_ADMIN,
  securityVersion: 1,
};
const configuration = {
  jwtAccessSecret: 'test-only-audit-cursor-secret-long-enough',
} as AppConfigService;

// 汇集审计权限、默认过滤、action 脱敏和游标安全测试。
describe('AuditService', () => {
  // 验证默认列表排除高频 refresh，并返回服务端映射而非原始 metadata。
  it('lists sanitized events while excluding routine refresh by default', async () => {
    const occurredAt = new Date('2026-07-28T08:00:00.000Z');
    const findMany = vi.fn().mockResolvedValue([
      {
        id: '00000000-0000-4000-8000-000000000020',
        actorId: superActor.userId,
        action: 'user.admin.updated',
        targetId: '00000000-0000-4000-8000-000000000002',
        requestId: 'request-1',
        metadata: { password: 'must-not-return' },
        occurredAt,
        actor: {
          id: superActor.userId,
          account: 'apex.admin',
          displayName: 'Apex Admin',
        },
      },
    ]);
    const service = auditWithFindMany(findMany);

    const result = await service.listEvents(superActor, {
      includeRoutine: false,
      pageSize: 20,
      occurredFrom: '2026-07-27T00:00:00.000Z',
      occurredTo: '2026-07-29T00:00:00.000Z',
    });

    expect(findMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: expect.objectContaining({
          action: { notIn: ['auth.refresh.rotated'] },
        }),
      }),
    );
    expect(result.items[0]).toEqual(
      expect.objectContaining({
        category: 'USER_ADMINISTRATION',
        severity: 'WARNING',
        summary: '管理员更新用户安全状态',
      }),
    );
    expect(result.items[0]).not.toHaveProperty('metadata');
  });

  // 验证 ADMIN 即使绕过 Controller 直接调用 Service 也被拒绝。
  it('rejects non-super-administrator readers before database access', async () => {
    const findMany = vi.fn();
    const service = auditWithFindMany(findMany);

    await expect(
      service.listEvents(
        { ...superActor, role: Role.ADMIN },
        { includeRoutine: false, pageSize: 20 },
      ),
    ).rejects.toMatchObject({ status: 403 });
    expect(findMany).not.toHaveBeenCalled();
  });

  // 验证详情只提取注册 action 的允许字段，秘密与未知字段全部丢弃。
  it('returns only action-specific allowlisted detail fields', async () => {
    const findUnique = vi.fn().mockResolvedValue({
      id: '00000000-0000-4000-8000-000000000020',
      actorId: superActor.userId,
      action: 'user.created',
      targetId: '00000000-0000-4000-8000-000000000002',
      requestId: 'request-1',
      metadata: {
        actorRole: Role.SUPER_ADMIN,
        account: 'ap***in',
        role: Role.ADMIN,
        password: 'must-not-return',
        token: 'must-not-return',
      },
      occurredAt: new Date('2026-07-28T08:00:00.000Z'),
      actor: null,
    });
    const service = new AuditService(
      { client: { auditLog: { findUnique } } } as unknown as DatabaseService,
      configuration,
    );

    const result = await service.getEvent(superActor, '00000000-0000-4000-8000-000000000020');

    expect(result.details).toEqual({
      actorRole: Role.SUPER_ADMIN,
      accountMasked: 'ap***in',
      assignedRole: Role.ADMIN,
    });
    expect(JSON.stringify(result)).not.toContain('must-not-return');
  });

  // 验证未知 action 使用通用映射与空详情，绝不把 metadata 当回退响应。
  it('maps unknown actions to generic empty detail', async () => {
    const findUnique = vi.fn().mockResolvedValue({
      id: '00000000-0000-4000-8000-000000000020',
      actorId: null,
      action: 'future.action',
      targetId: null,
      requestId: null,
      metadata: { secret: 'must-not-return' },
      occurredAt: new Date('2026-07-28T08:00:00.000Z'),
      actor: null,
    });
    const service = new AuditService(
      { client: { auditLog: { findUnique } } } as unknown as DatabaseService,
      configuration,
    );

    const result = await service.getEvent(superActor, '00000000-0000-4000-8000-000000000020');

    expect(result).toEqual(
      expect.objectContaining({
        category: 'SYSTEM',
        severity: 'INFO',
        summary: '未识别的审计操作',
        details: {},
      }),
    );
    expect(JSON.stringify(result)).not.toContain('must-not-return');
  });

  // 验证反向或超过九十天时间窗在数据库查询前返回稳定 400。
  it('rejects invalid audit time windows', async () => {
    const findMany = vi.fn();
    const service = auditWithFindMany(findMany);

    await expect(
      service.listEvents(superActor, {
        includeRoutine: false,
        pageSize: 20,
        occurredFrom: '2026-07-29T00:00:00.000Z',
        occurredTo: '2026-07-28T00:00:00.000Z',
      }),
    ).rejects.toMatchObject({ status: 400 });
    expect(findMany).not.toHaveBeenCalled();
  });

  // 验证无法通过签名的游标不会进入数据库查询。
  it('rejects tampered cursors', async () => {
    const findMany = vi.fn();
    const service = auditWithFindMany(findMany);

    await expect(
      service.listEvents(superActor, {
        includeRoutine: false,
        pageSize: 20,
        cursor: Buffer.from('{"id":"tampered"}').toString('base64url'),
      }),
    ).rejects.toMatchObject({ status: 400 });
    expect(findMany).not.toHaveBeenCalled();
  });
});

/** 构造只注入审计列表查询替身的 AuditService。 */
function auditWithFindMany(findMany: ReturnType<typeof vi.fn>): AuditService {
  return new AuditService(
    { client: { auditLog: { findMany } } } as unknown as DatabaseService,
    configuration,
  );
}
