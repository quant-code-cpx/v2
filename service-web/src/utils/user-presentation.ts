import type { UserRole, UserStatus } from "../types/access";

/** Translate fixed server roles into concise, non-authoritative interface labels. */
export function userRoleLabel(role: UserRole): string {
  const labels: Record<UserRole, string> = {
    USER: "普通用户",
    ADMIN: "管理员",
    SUPER_ADMIN: "超级管理员",
  };

  return labels[role];
}

/** Translate lifecycle states into visible labels paired with status chips. */
export function userStatusLabel(status: UserStatus): string {
  const labels: Record<UserStatus, string> = {
    ACTIVE: "启用",
    DISABLED: "已禁用",
    DELETED: "已删除",
  };

  return labels[status];
}

/** Mask an account for diagnostics when a support-facing identifier is necessary. */
export function maskAccount(account: string): string {
  if (account.length <= 2) {
    return "**";
  }

  return `${account.slice(0, 2)}***${account.slice(-2)}`;
}
