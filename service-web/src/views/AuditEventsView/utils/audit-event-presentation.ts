import type {
  AuditCategory,
  AuditEventDetail,
  AuditSeverity,
} from "../../../types/account-security";

/** 返回审计分类的稳定中文标签。 */
export function auditCategoryLabel(category: AuditCategory): string {
  const labels: Record<AuditCategory, string> = {
    AUTHENTICATION: "认证安全",
    ACCOUNT: "账户安全",
    USER_ADMINISTRATION: "用户管理",
    SYSTEM: "系统",
  };

  return labels[category];
}

/** 返回审计严重级别的稳定中文标签。 */
export function auditSeverityLabel(severity: AuditSeverity): string {
  const labels: Record<AuditSeverity, string> = {
    INFO: "信息",
    WARNING: "警告",
    CRITICAL: "严重",
  };

  return labels[severity];
}

/** 返回详情 allowlist 的可显示键值，不暴露未知字段。 */
export function auditDetailEntries(detail: AuditEventDetail): Array<[string, string]> {
  const entries: Array<[string, string]> = [];

  if (detail.details.actorRole !== undefined) {
    entries.push(["操作角色", detail.details.actorRole]);
  }
  if (detail.details.accountMasked !== undefined) {
    entries.push(["脱敏账号", detail.details.accountMasked]);
  }
  if (detail.details.assignedRole !== undefined) {
    entries.push(["分配角色", detail.details.assignedRole]);
  }
  if (detail.details.before !== undefined) {
    entries.push(["变更前", formatSecuritySnapshot(detail.details.before)]);
  }
  if (detail.details.after !== undefined) {
    entries.push(["变更后", formatSecuritySnapshot(detail.details.after)]);
  }
  if (detail.details.revokedFamilyCount !== undefined) {
    entries.push(["撤销会话数", String(detail.details.revokedFamilyCount)]);
  }

  return entries;
}

/** 将服务端允许的安全快照格式化为无歧义文本。 */
function formatSecuritySnapshot(snapshot: { role?: string; status?: string }): string {
  return [snapshot.role, snapshot.status].filter((value) => value !== undefined).join(" · ") || "—";
}
