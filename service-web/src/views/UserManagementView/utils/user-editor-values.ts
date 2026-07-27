import type { UserRole, UserStatus } from "../../../types/access";

/** 描述用户编辑表单的本地字段，包含仅创建时存在的短期密码。 */
export interface UserEditorValues {
  account: string;
  displayName: string;
  password: string;
  role: Extract<UserRole, "USER" | "ADMIN">;
  status: Extract<UserStatus, "ACTIVE" | "DISABLED">;
}

/** 描述用户编辑表单的字段级错误。 */
export interface UserEditorFieldErrors {
  account?: string;
  displayName?: string;
  password?: string;
}

/** 创建保守角色与状态默认值的空表单。 */
export function createEmptyUserEditorValues(): UserEditorValues {
  return {
    account: "",
    displayName: "",
    password: "",
    role: "USER",
    status: "ACTIVE",
  };
}

/** 将用户资源映射为不含密码的安全编辑值。 */
export function userToEditorValues(user: {
  account: string;
  displayName: string;
  role: UserRole;
  status: UserStatus;
}): UserEditorValues {
  return {
    account: user.account,
    displayName: user.displayName,
    password: "",
    role: user.role === "ADMIN" ? "ADMIN" : "USER",
    status: user.status === "DISABLED" ? "DISABLED" : "ACTIVE",
  };
}
