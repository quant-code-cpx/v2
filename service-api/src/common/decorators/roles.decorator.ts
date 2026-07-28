import { SetMetadata } from '@nestjs/common';

import type { Role } from '../../generated/prisma/client.js';

export const ROLES_KEY = 'roles';

/** Attach coarse role requirements; UserModule still enforces target-level policy in its use cases. */
export const Roles = (...roles: Role[]): ReturnType<typeof SetMetadata> =>
  SetMetadata(ROLES_KEY, roles);
