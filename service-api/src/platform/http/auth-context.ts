import type { Request } from 'express';

import type { Role } from '../../generated/prisma/client.js';

/** Carry only server-validated authorization state required by protected HTTP handlers. */
export type AuthContext = {
  userId: string;
  sessionId: string;
  role: Role;
  securityVersion: number;
};

/** Extend Express requests after the global authentication guard validates a bearer token. */
export type AuthenticatedRequest = Request & {
  user: AuthContext;
};
