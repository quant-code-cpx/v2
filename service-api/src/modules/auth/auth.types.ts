import type { Role } from '../../generated/prisma/client.js';
import type { CurrentUserResource } from '../user/user.types.js';

export type { AuthContext, AuthenticatedRequest } from '../../platform/http/auth-context.js';

export type JwtPayload = {
  sub: string;
  sid: string;
  role: Role;
  sv: number;
};

export type TokenPair = {
  accessToken: string;
  refreshToken: string;
  refreshExpiresAt: Date;
  user: CurrentUserResource;
};
