import type { Role, UserStatus } from '../../generated/prisma/client.js';

export type AuthenticatedUser = {
  id: string;
  email: string;
  displayName: string;
  role: Role;
  status: UserStatus;
  securityVersion: number;
};

export type UserResource = {
  id: string;
  email: string;
  displayName: string;
  role: Role;
  status: UserStatus;
  createdAt: string;
  updatedAt: string;
};

export type UserPage = {
  items: UserResource[];
  page: {
    nextCursor: string | null;
  };
};
