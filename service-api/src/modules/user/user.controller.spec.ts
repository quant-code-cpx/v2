import { Role } from '../../generated/prisma/client.js';
import type { AuthenticatedRequest } from '../../platform/http/auth-context.js';
import type { AppConfigService } from '../../platform/config/app-config.service.js';
import { describe, expect, it, vi } from 'vitest';

import { UserController } from './user.controller.js';
import type { UserService } from './user.service.js';

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

// Group controller-level If-Match precondition regressions before UserService mutation is invoked.
describe('UserController ETag handling', () => {
  // Verify missing If-Match returns 428 rather than allowing a blind administrator update.
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

  // Verify an ETag from another resource fails 412 before target mutation is attempted.
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

/** Return the minimal Express response fixture needed by passthrough handlers in precondition tests. */
function response() {
  return { setHeader: vi.fn(), clearCookie: vi.fn() } as never;
}
