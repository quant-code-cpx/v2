import { Role } from '../../../generated/prisma/client.js';
import type { AuthenticatedRequest } from '../../../platform/http/auth-context.js';
import type { AppConfigService } from '../../../platform/config/app-config.service.js';
import { describe, expect, it, vi } from 'vitest';

import { UserController } from '../user.controller.js';
import type { UserService } from '../user.service.js';

const userId = '00000000-0000-4000-8000-000000000001';
const request = {
  user: {
    userId,
    sessionId: '00000000-0000-4000-8000-000000000010',
    role: Role.ADMIN,
    securityVersion: 1,
  },
  requestId: 'request-1',
} as AuthenticatedRequest;

// 汇集调用 UserService 变更前的 Controller 级 If-Match 前置条件回归测试。
describe('UserController ETag handling', () => {
  // 验证缺少 If-Match 时返回 428，而不是允许管理员盲写。
  it('rejects missing If-Match before target update', async () => {
    const updateUser = vi.fn();
    const controller = new UserController(
      { updateUser } as unknown as UserService,
      { apiPrefix: 'api/v1' } as AppConfigService,
    );

    await expect(
      controller.update(request, userId, undefined, { displayName: 'Changed' }, response()),
    ).rejects.toMatchObject({ status: 428 });
    expect(updateUser).not.toHaveBeenCalled();
  });

  // 验证来自其他资源的 ETag 在尝试目标变更前返回 412。
  it('rejects cross-resource If-Match before target update', async () => {
    const updateUser = vi.fn();
    const controller = new UserController(
      { updateUser } as unknown as UserService,
      { apiPrefix: 'api/v1' } as AppConfigService,
    );

    await expect(
      controller.update(
        request,
        userId,
        '"user-00000000-0000-4000-8000-000000000099-v1"',
        { displayName: 'Changed' },
        response(),
      ),
    ).rejects.toMatchObject({ status: 412 });
    expect(updateUser).not.toHaveBeenCalled();
  });
});

/** 返回前置条件测试中透传 handler 所需的最小 Express 响应 fixture。 */
function response() {
  return { setHeader: vi.fn(), clearCookie: vi.fn() } as never;
}
