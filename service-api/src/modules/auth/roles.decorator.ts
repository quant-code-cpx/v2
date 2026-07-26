import { SetMetadata } from '@nestjs/common';

import type { Role } from '../../generated/prisma/client.js';

export const ROLES_KEY = 'roles';

/** Attach allowed roles for `RolesGuard` to current controller or route. */
export const Roles = (...roles: Role[]): ReturnType<typeof SetMetadata> =>
  SetMetadata(ROLES_KEY, roles);
