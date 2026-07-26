import type { Request } from 'express';

import type { Role } from '../../generated/prisma/client.js';

export type JwtPayload = {
  sub: string;
  sid: string;
  role: Role;
  sv: number;
};

export type AuthContext = {
  userId: string;
  sessionId: string;
  role: Role;
};

export type AuthenticatedRequest = Request & {
  user: AuthContext;
};

export type TokenPair = {
  accessToken: string;
  refreshToken: string;
  refreshExpiresAt: Date;
};
