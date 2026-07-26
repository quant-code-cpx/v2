import type { Role, UserStatus } from '../../generated/prisma/client.js';

export type AuthenticatedUser = {
  id: string;
  account: string;
  displayName: string;
  role: Role;
  status: UserStatus;
  securityVersion: number;
};

export type Permission =
  | 'profile:read'
  | 'profile:update'
  | 'password:change'
  | 'users:read'
  | 'users:create'
  | 'users:update'
  | 'users:delete'
  | 'users:reset-password'
  | 'admins:create'
  | 'admins:manage';

export type UserResource = {
  id: string;
  account: string;
  displayName: string;
  role: Role;
  status: UserStatus;
  version: number;
  lastLoginAt: string | null;
  deletedAt: string | null;
  createdAt: string;
  updatedAt: string;
};

export type CurrentUserResource = UserResource & {
  permissions: Permission[];
};

export type UserPage = {
  items: UserResource[];
  page: {
    nextCursor: string | null;
  };
};
