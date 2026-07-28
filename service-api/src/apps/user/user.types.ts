import type { Role, UserStatus } from '../../generated/prisma/client.js';

/** 表示鉴权流程使用的最小可变用户安全快照。 */
export type AuthenticatedUser = {
  id: string;
  account: string;
  displayName: string;
  role: Role;
  status: UserStatus;
  securityVersion: number;
};

/** 定义服务端按角色计算并返回给 UI 的能力标识。 */
export type Permission =
  | 'profile:read'
  | 'profile:update'
  | 'password:change'
  | 'sessions:read'
  | 'sessions:revoke'
  | 'users:read'
  | 'users:create'
  | 'users:update'
  | 'users:delete'
  | 'users:reset-password'
  | 'admins:create'
  | 'admins:manage'
  | 'audit:read';

/** 表示不含凭据与 securityVersion 的公开用户资源。 */
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

/** 表示当前用户资源及服务端计算的有效权限。 */
export type CurrentUserResource = UserResource & {
  permissions: Permission[];
};

/** 表示用户管理列表的有界游标页。 */
export type UserPage = {
  items: UserResource[];
  page: {
    nextCursor: string | null;
  };
};

/** 表示一个可管理角色的用户状态计数。 */
export type RoleStatistics = {
  role: Exclude<Role, 'SUPER_ADMIN'>;
  total: number;
  active: number;
  disabled: number;
  deleted: number;
};

/** 表示调用方角色范围内的用户聚合统计，不包含任何个体记录。 */
export type ManageableUserStatistics = {
  generatedAt: string;
  scope: Array<Exclude<Role, 'SUPER_ADMIN'>>;
  total: number;
  active: number;
  disabled: number;
  deleted: number;
  loggedInLast30Days: number;
  byRole: RoleStatistics[];
};
