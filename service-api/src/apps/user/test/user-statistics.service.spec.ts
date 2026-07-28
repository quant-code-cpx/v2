import { Role, UserStatus } from '../../../generated/prisma/client.js';
import type { AuthContext } from '../../../common/models/auth-context.js';
import type { AppConfigService } from '../../../config/app-config.service.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import { describe, expect, it, vi } from 'vitest';

import { permissionsForRole, UserService } from '../user.service.js';

const configuration = {
  jwtAccessSecret: 'test-only-user-cursor-secret-long-enough',
} as AppConfigService;

// 汇集可管理用户统计范围和新增权限矩阵测试。
describe('UserService manageable statistics', () => {
  // 验证 ADMIN 统计只包含 USER，并按状态补齐聚合值。
  it('counts only USER targets for an administrator', async () => {
    const actor = authContext(Role.ADMIN);
    const groupBy = vi.fn().mockResolvedValue([
      { role: Role.USER, status: UserStatus.ACTIVE, _count: { _all: 4 } },
      { role: Role.USER, status: UserStatus.DISABLED, _count: { _all: 1 } },
    ]);
    const count = vi.fn().mockResolvedValue(3);
    const service = statisticsService(groupBy, count);

    const result = await service.getManageableStatistics(actor);

    expect(result).toEqual(
      expect.objectContaining({
        scope: [Role.USER],
        total: 5,
        active: 4,
        disabled: 1,
        deleted: 0,
        loggedInLast30Days: 3,
        byRole: [
          {
            role: Role.USER,
            total: 5,
            active: 4,
            disabled: 1,
            deleted: 0,
          },
        ],
      }),
    );
    expect(groupBy).toHaveBeenCalledWith(
      expect.objectContaining({
        where: {
          id: { not: actor.userId },
          role: { in: [Role.USER] },
        },
      }),
    );
  });

  // 验证 SUPER_ADMIN 同时获得 USER 与 ADMIN 两个明确范围。
  it('counts USER and ADMIN targets for a super administrator', async () => {
    const actor = authContext(Role.SUPER_ADMIN);
    const groupBy = vi.fn().mockResolvedValue([
      { role: Role.USER, status: UserStatus.ACTIVE, _count: { _all: 2 } },
      { role: Role.ADMIN, status: UserStatus.DISABLED, _count: { _all: 1 } },
    ]);
    const service = statisticsService(groupBy, vi.fn().mockResolvedValue(1));

    const result = await service.getManageableStatistics(actor);

    expect(result.scope).toEqual([Role.USER, Role.ADMIN]);
    expect(result.total).toBe(3);
    expect(result.byRole).toEqual([
      { role: Role.USER, total: 2, active: 2, disabled: 0, deleted: 0 },
      { role: Role.ADMIN, total: 1, active: 0, disabled: 1, deleted: 0 },
    ]);
  });

  // 验证普通用户不能通过直接 Service 调用读取运营统计。
  it('rejects ordinary users before aggregation', async () => {
    const groupBy = vi.fn();
    const service = statisticsService(groupBy, vi.fn());

    await expect(service.getManageableStatistics(authContext(Role.USER))).rejects.toMatchObject({
      status: 403,
    });
    expect(groupBy).not.toHaveBeenCalled();
  });
});

// 汇集角色到冻结权限数组的回归测试。
describe('permissionsForRole account security additions', () => {
  // 验证所有角色都有本人会话权限，但只有超级管理员可读审计。
  it('grants session permissions to all roles and audit read only to SUPER_ADMIN', () => {
    expect(permissionsForRole(Role.USER)).toEqual(
      expect.arrayContaining(['sessions:read', 'sessions:revoke']),
    );
    expect(permissionsForRole(Role.ADMIN)).not.toContain('audit:read');
    expect(permissionsForRole(Role.SUPER_ADMIN)).toContain('audit:read');
  });
});

/** 构造固定角色的已认证请求上下文。 */
function authContext(role: Role): AuthContext {
  return {
    userId: '00000000-0000-4000-8000-000000000001',
    sessionId: '00000000-0000-4000-8000-000000000010',
    role,
    securityVersion: 1,
  };
}

/** 构造只注入 groupBy 与 count 查询的 UserService。 */
function statisticsService(
  groupBy: ReturnType<typeof vi.fn>,
  count: ReturnType<typeof vi.fn>,
): UserService {
  const transaction = { user: { groupBy, count } };
  return new UserService(
    {
      client: {
        // 统计使用同一事务快照读取状态分组和近三十天登录数。
        $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
          callback(transaction),
      },
    } as unknown as DatabaseService,
    configuration,
  );
}
