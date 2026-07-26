/** Enumerate fixed roles returned by User/Auth Contract 0002. */
export const userRoles = ["USER", "ADMIN", "SUPER_ADMIN"] as const;

/** Represent one fixed server-enforced role. */
export type UserRole = (typeof userRoles)[number];

/** Enumerate lifecycle states exposed by User/Auth Contract 0002. */
export const userStatuses = ["ACTIVE", "DISABLED", "DELETED"] as const;

/** Represent one server-enforced user lifecycle state. */
export type UserStatus = (typeof userStatuses)[number];

/** Enumerate capability strings used for Web visibility and route checks. */
export const permissions = [
  "profile:read",
  "profile:update",
  "password:change",
  "users:read",
  "users:create",
  "users:update",
  "users:delete",
  "users:reset-password",
  "admins:create",
  "admins:manage",
] as const;

/** Represent a capability calculated and returned by service-api. */
export type Permission = (typeof permissions)[number];

/** Describe a non-sensitive user resource returned by service-api. */
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

/** Describe current session identity with effective permissions. */
export interface CurrentUser extends User {
  permissions: Permission[];
}

/** Describe backend-rendered, single-use PNG CAPTCHA state. */
export interface CaptchaChallenge {
  challengeId: string;
  imageDataUrl: string;
  expiresAt: string;
}

/** Describe short-lived access token response paired with a refresh cookie. */
export interface AccessTokenResponse {
  accessToken: string;
  accessTokenExpiresIn: number;
  user: CurrentUser;
}

/** Describe a cursor page of user resources. */
export interface UserPage {
  items: User[];
  page: {
    nextCursor: string | null;
  };
}

/** Describe validated list filters represented in the browser URL. */
export interface UserListFilters {
  q?: string;
  role?: Extract<UserRole, "USER" | "ADMIN">;
  status?: UserStatus;
  sort: "createdAt" | "updatedAt" | "account" | "displayName";
  order: "asc" | "desc";
  cursor?: string;
  pageSize: number;
}

/** Describe credentials accepted only by the anonymous login endpoint. */
export interface LoginInput {
  account: string;
  password: string;
  captchaId: string;
  captchaAnswer: string;
}

/** Describe administrator-supplied initial user fields. */
export interface CreateUserInput {
  account: string;
  displayName: string;
  password: string;
  role: Extract<UserRole, "USER" | "ADMIN">;
  status: Extract<UserStatus, "ACTIVE" | "DISABLED">;
}

/** Describe mutable administrator-controlled user fields. */
export interface UpdateUserInput {
  displayName: string;
  role: Extract<UserRole, "USER" | "ADMIN">;
  status: Extract<UserStatus, "ACTIVE" | "DISABLED">;
}
