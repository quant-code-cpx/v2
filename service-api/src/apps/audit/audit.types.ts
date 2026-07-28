import type { Role, UserStatus } from '../../generated/prisma/client.js';

/** 审计事件的稳定业务分类。 */
export type AuditCategory = 'AUTHENTICATION' | 'ACCOUNT' | 'USER_ADMINISTRATION' | 'SYSTEM';

/** 审计事件面向运营读取的风险级别。 */
export type AuditSeverity = 'INFO' | 'WARNING' | 'CRITICAL';

/** 审计目标的最小化资源类别。 */
export type AuditTargetType = 'USER' | 'SESSION' | 'SYSTEM' | 'UNKNOWN';

/** 审计事件中允许返回的调用方身份快照。 */
export type AuditActorResource = {
  id: string;
  account: string;
  displayName: string;
};

/** 审计详情严格允许的字段集合，禁止承载原始 metadata。 */
export type AuditDetails = {
  actorRole?: Role;
  accountMasked?: string;
  assignedRole?: Exclude<Role, 'SUPER_ADMIN'>;
  before?: {
    role?: Role;
    status?: UserStatus;
  };
  after?: {
    role?: Role;
    status?: UserStatus;
  };
  revokedFamilyCount?: number;
};

/** 表示一个经过服务端 action registry 脱敏映射的审计事件。 */
export type AuditEventResource = {
  id: string;
  category: AuditCategory;
  severity: AuditSeverity;
  action: string;
  summary: string;
  actor: AuditActorResource | null;
  target: {
    type: AuditTargetType;
    id: string | null;
  };
  requestId: string | null;
  occurredAt: string;
};

/** 表示带动作专属白名单字段的审计详情。 */
export type AuditEventDetail = AuditEventResource & {
  details: AuditDetails;
};

/** 表示审计查询实际采用的闭合时间窗口。 */
export type AppliedTimeWindow = {
  occurredFrom: string;
  occurredTo: string;
};

/** 表示脱敏审计事件的有界游标页。 */
export type AuditEventPage = {
  items: AuditEventResource[];
  page: {
    nextCursor: string | null;
  };
  appliedWindow: AppliedTimeWindow;
};
