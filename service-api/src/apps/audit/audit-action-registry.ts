import { Role, UserStatus, type Prisma } from '../../generated/prisma/client.js';
import type { AuditCategory, AuditDetails, AuditSeverity, AuditTargetType } from './audit.types.js';

/** 描述一个审计 action 的公开映射规则与详情白名单。 */
export type AuditActionDefinition = {
  category: AuditCategory;
  severity: AuditSeverity;
  summary: string;
  targetType: AuditTargetType;
  routine?: boolean;
  details: (metadata: Prisma.JsonValue | null) => AuditDetails;
};

/** 对不需要详情的 action 返回稳定空对象。 */
function emptyDetails(metadata: Prisma.JsonValue | null): AuditDetails {
  void metadata;
  return {};
}

/** 提取撤销其他会话动作允许公开的聚合数量。 */
function revokedFamilyDetails(metadata: Prisma.JsonValue | null): AuditDetails {
  const value = record(metadata)?.revokedFamilyCount;
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? { revokedFamilyCount: value }
    : {};
}

/** 提取用户创建动作允许公开的角色与已脱敏账号。 */
function createdUserDetails(metadata: Prisma.JsonValue | null): AuditDetails {
  const value = record(metadata);
  if (!value) {
    return {};
  }
  return {
    ...roleField(value.actorRole, 'actorRole'),
    ...manageableRoleField(value.role),
    ...maskedAccountField(value.account),
  };
}

/** 提取用户安全状态更新动作允许公开的前后快照。 */
function updatedUserDetails(metadata: Prisma.JsonValue | null): AuditDetails {
  const value = record(metadata);
  if (!value) {
    return {};
  }
  return {
    ...roleField(value.actorRole, 'actorRole'),
    ...securitySnapshotField(value.before, 'before'),
    ...securitySnapshotField(value.after, 'after'),
  };
}

/** 提取仅含调用角色与脱敏账号的管理员动作详情。 */
function managedAccountDetails(metadata: Prisma.JsonValue | null): AuditDetails {
  const value = record(metadata);
  if (!value) {
    return {};
  }
  return {
    ...roleField(value.actorRole, 'actorRole'),
    ...maskedAccountField(value.account),
  };
}

/** 提取系统初始化动作中已经脱敏的账号标识。 */
function bootstrapDetails(metadata: Prisma.JsonValue | null): AuditDetails {
  const value = record(metadata);
  return value ? maskedAccountField(value.account) : {};
}

/** 将 JSON 对象安全收窄为只读字段字典。 */
function record(
  value: Prisma.JsonValue | null | undefined,
): Record<string, Prisma.JsonValue> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, Prisma.JsonValue>)
    : null;
}

/** 仅接受 Prisma 角色枚举中的合法值。 */
function role(value: Prisma.JsonValue | undefined): Role | undefined {
  return typeof value === 'string' && Object.values(Role).includes(value as Role)
    ? (value as Role)
    : undefined;
}

/** 仅接受可管理目标角色，禁止把 SUPER_ADMIN 当作新建目标返回。 */
function manageableRoleField(
  value: Prisma.JsonValue | undefined,
): Pick<AuditDetails, 'assignedRole'> {
  const parsed = role(value);
  return parsed === Role.USER || parsed === Role.ADMIN ? { assignedRole: parsed } : {};
}

/** 将合法角色写入指定详情字段。 */
function roleField(
  value: Prisma.JsonValue | undefined,
  field: 'actorRole',
): Pick<AuditDetails, 'actorRole'> {
  const parsed = role(value);
  return parsed === undefined ? {} : { [field]: parsed };
}

/** 只返回已经在写入端脱敏且长度有界的账号字段。 */
function maskedAccountField(
  value: Prisma.JsonValue | undefined,
): Pick<AuditDetails, 'accountMasked'> {
  return typeof value === 'string' && value.length <= 32 ? { accountMasked: value } : {};
}

/** 从动作 metadata 中提取合法的用户安全状态快照。 */
function securitySnapshotField(
  value: Prisma.JsonValue | undefined,
  field: 'before' | 'after',
): Pick<AuditDetails, 'before' | 'after'> {
  const source = record(value);
  if (!source) {
    return {};
  }
  const parsedRole = role(source.role);
  const parsedStatus =
    typeof source.status === 'string' &&
    Object.values(UserStatus).includes(source.status as UserStatus)
      ? (source.status as UserStatus)
      : undefined;
  if (parsedRole === undefined && parsedStatus === undefined) {
    return {};
  }
  return {
    [field]: {
      ...(parsedRole === undefined ? {} : { role: parsedRole }),
      ...(parsedStatus === undefined ? {} : { status: parsedStatus }),
    },
  };
}

/** 服务端拥有的完整 action registry；未知 action 不回退到原始 metadata。 */
export const AUDIT_ACTION_REGISTRY: Readonly<Record<string, AuditActionDefinition>> = {
  'auth.login.succeeded': {
    category: 'AUTHENTICATION',
    severity: 'INFO',
    summary: '用户登录成功',
    targetType: 'USER',
    details: emptyDetails,
  },
  'auth.logout': {
    category: 'AUTHENTICATION',
    severity: 'INFO',
    summary: '用户退出登录',
    targetType: 'SESSION',
    details: emptyDetails,
  },
  'auth.refresh.rotated': {
    category: 'AUTHENTICATION',
    severity: 'INFO',
    summary: '会话凭据已轮换',
    targetType: 'SESSION',
    routine: true,
    details: emptyDetails,
  },
  'auth.refresh.replay_detected': {
    category: 'AUTHENTICATION',
    severity: 'CRITICAL',
    summary: '检测到会话凭据重放',
    targetType: 'SESSION',
    details: emptyDetails,
  },
  'auth.session_family.revoked': {
    category: 'AUTHENTICATION',
    severity: 'WARNING',
    summary: '用户撤销一个会话',
    targetType: 'SESSION',
    details: emptyDetails,
  },
  'auth.session_families.others_revoked': {
    category: 'AUTHENTICATION',
    severity: 'WARNING',
    summary: '用户撤销其他会话',
    targetType: 'USER',
    details: revokedFamilyDetails,
  },
  'user.profile.updated': {
    category: 'ACCOUNT',
    severity: 'INFO',
    summary: '用户资料已更新',
    targetType: 'USER',
    details: emptyDetails,
  },
  'user.password.changed': {
    category: 'ACCOUNT',
    severity: 'WARNING',
    summary: '用户密码已修改',
    targetType: 'USER',
    details: emptyDetails,
  },
  'user.created': {
    category: 'USER_ADMINISTRATION',
    severity: 'INFO',
    summary: '管理员创建用户',
    targetType: 'USER',
    details: createdUserDetails,
  },
  'user.admin.updated': {
    category: 'USER_ADMINISTRATION',
    severity: 'WARNING',
    summary: '管理员更新用户安全状态',
    targetType: 'USER',
    details: updatedUserDetails,
  },
  'user.deleted': {
    category: 'USER_ADMINISTRATION',
    severity: 'CRITICAL',
    summary: '管理员删除用户',
    targetType: 'USER',
    details: managedAccountDetails,
  },
  'user.password.reset': {
    category: 'USER_ADMINISTRATION',
    severity: 'WARNING',
    summary: '管理员重置用户密码',
    targetType: 'USER',
    details: managedAccountDetails,
  },
  'system.bootstrap.super_admin_created': {
    category: 'SYSTEM',
    severity: 'CRITICAL',
    summary: '系统初始化超级管理员',
    targetType: 'USER',
    details: bootstrapDetails,
  },
  'system.bootstrap.super_admin_promoted': {
    category: 'SYSTEM',
    severity: 'CRITICAL',
    summary: '系统提升超级管理员',
    targetType: 'USER',
    details: bootstrapDetails,
  },
  'dataops.request.authorized': {
    category: 'SYSTEM',
    severity: 'INFO',
    summary: '数据运维操作已授权',
    targetType: 'SYSTEM',
    details: emptyDetails,
  },
  'dataops.delivery.accepted': {
    category: 'SYSTEM',
    severity: 'INFO',
    summary: '数据运维操作已由权威服务受理',
    targetType: 'SYSTEM',
    details: emptyDetails,
  },
  'dataops.delivery.rejected': {
    category: 'SYSTEM',
    severity: 'WARNING',
    summary: '数据运维操作被权威服务拒绝',
    targetType: 'SYSTEM',
    details: emptyDetails,
  },
  'dataops.delivery.dead_lettered': {
    category: 'SYSTEM',
    severity: 'CRITICAL',
    summary: '数据运维可靠投递进入死信',
    targetType: 'SYSTEM',
    details: emptyDetails,
  },
  'dataops.delivery.replayed': {
    category: 'SYSTEM',
    severity: 'WARNING',
    summary: '数据运维死信投递已重放',
    targetType: 'SYSTEM',
    details: emptyDetails,
  },
};

/** 返回指定分类当前注册的 action 集合，供数据库预过滤。 */
export function actionsForCategory(category: AuditCategory): string[] {
  const entries = Object.entries(AUDIT_ACTION_REGISTRY);
  // 分类筛选只读取服务端 registry，不根据 metadata 推断。
  const matching = entries.filter(([, definition]) => definition.category === category);
  // 仅把匹配定义投影为数据库 action 筛选值。
  return matching.map(([action]) => action);
}

/** 返回默认列表需要排除的高频例行 action 集合。 */
export function routineAuditActions(): string[] {
  const entries = Object.entries(AUDIT_ACTION_REGISTRY);
  // 例行筛选由每个 action 定义的显式标记控制。
  const routine = entries.filter(([, definition]) => definition.routine === true);
  // 仅把例行定义投影为数据库 action 筛选值。
  return routine.map(([action]) => action);
}
