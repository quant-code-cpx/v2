/** 枚举用户与鉴权合同返回的固定角色。 */
export const userRoles = ["USER", "ADMIN", "SUPER_ADMIN"] as const;

/** 表示一个由服务端强制执行的固定角色。 */
export type UserRole = (typeof userRoles)[number];

/** 枚举用户与鉴权合同公开的生命周期状态。 */
export const userStatuses = ["ACTIVE", "DISABLED", "DELETED"] as const;

/** 表示一个由服务端强制执行的用户生命周期状态。 */
export type UserStatus = (typeof userStatuses)[number];

/** 枚举用于 Web 可见性与路由检查的服务端能力字符串。 */
export const permissions = [
  "profile:read",
  "profile:update",
  "password:change",
  "sessions:read",
  "sessions:revoke",
  "users:read",
  "users:create",
  "users:update",
  "users:delete",
  "users:reset-password",
  "admins:create",
  "admins:manage",
  "audit:read",
] as const;

/** 表示由 `service-api` 计算并返回的能力。 */
export type Permission = (typeof permissions)[number];

/** 描述 `service-api` 返回的非敏感用户资源。 */
export interface User {
  id: string;
  account: string;
  displayName: string;
  role: UserRole;
  status: UserStatus;
  version: number;
  lastLoginAt: string | null;
  deletedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

/** 描述当前会话身份及其有效权限。 */
export interface CurrentUser extends User {
  permissions: Permission[];
}

/** 描述后端渲染的一次性 PNG 验证码状态。 */
export interface CaptchaChallenge {
  challengeId: string;
  imageDataUrl: string;
  expiresAt: string;
}

/** 描述与 refresh cookie 配对的短期 access token 响应。 */
export interface AccessTokenResponse {
  accessToken: string;
  accessTokenExpiresIn: number;
  user: CurrentUser;
}

/** 描述用户资源的游标分页。 */
export interface UserPage {
  items: User[];
  page: {
    nextCursor: string | null;
  };
}

/** 描述浏览器 URL 表达的已校验用户列表筛选。 */
export interface UserListFilters {
  q?: string;
  role?: Extract<UserRole, "USER" | "ADMIN">;
  status?: UserStatus;
  sort: "createdAt" | "updatedAt" | "account" | "displayName";
  order: "asc" | "desc";
  cursor?: string;
  pageSize: number;
}

/** 描述仅匿名登录端点接受的凭据。 */
export interface LoginInput {
  account: string;
  password: string;
  captchaId: string;
  captchaAnswer: string;
}

/** 描述管理员提供的初始用户字段。 */
export interface CreateUserInput {
  account: string;
  displayName: string;
  password: string;
  role: Extract<UserRole, "USER" | "ADMIN">;
  status: Extract<UserStatus, "ACTIVE" | "DISABLED">;
}

/** 描述管理员可修改的用户字段。 */
export interface UpdateUserInput {
  displayName: string;
  role: Extract<UserRole, "USER" | "ADMIN">;
  status: Extract<UserStatus, "ACTIVE" | "DISABLED">;
}
