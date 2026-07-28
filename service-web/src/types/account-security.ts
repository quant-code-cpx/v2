import { z } from "zod";

import { userRoles, userStatuses } from "./access";

/** 校验合同 0017 使用的 UUID 标识。 */
const uuidSchema = z.string().uuid();

/** 校验合同 0017 使用的带时区 ISO 时间。 */
const dateTimeSchema = z.string().datetime({ offset: true });

/** 校验游标分页元数据。 */
const cursorPageSchema = z
  .object({
    nextCursor: z.string().max(512).nullable(),
  })
  .strict();

/** 校验一个不含设备、IP 或位置的活动 Session family。 */
export const sessionFamilySchema = z
  .object({
    familyId: uuidSchema,
    current: z.boolean(),
    lastActiveAt: dateTimeSchema,
    absoluteExpiresAt: dateTimeSchema,
  })
  .strict();

/** 校验本人活动 Session family 分页响应。 */
export const sessionFamilyPageSchema = z
  .object({
    items: z.array(sessionFamilySchema).max(50),
    page: cursorPageSchema,
    total: z.number().int().nonnegative(),
  })
  .strict();

/** 校验退出其他 Session family 的聚合结果。 */
export const revokeOtherSessionsResultSchema = z
  .object({
    revokedFamilyCount: z.number().int().nonnegative(),
  })
  .strict();

/** 枚举合同 0017 公开的审计分类。 */
export const auditCategories = [
  "AUTHENTICATION",
  "ACCOUNT",
  "USER_ADMINISTRATION",
  "SYSTEM",
] as const;

/** 表示一个审计分类。 */
export type AuditCategory = (typeof auditCategories)[number];

/** 枚举合同 0017 公开的审计严重级别。 */
export const auditSeverities = ["INFO", "WARNING", "CRITICAL"] as const;

/** 表示一个审计严重级别。 */
export type AuditSeverity = (typeof auditSeverities)[number];

/** 校验服务端净化后的审计 Actor。 */
const auditActorSchema = z
  .object({
    id: uuidSchema,
    account: z.string().min(5).max(32),
    displayName: z.string().min(1).max(120),
  })
  .strict();

/** 校验服务端净化后的审计目标。 */
const auditTargetSchema = z
  .object({
    type: z.enum(["USER", "SESSION", "SYSTEM", "UNKNOWN"]),
    id: uuidSchema.nullable(),
  })
  .strict();

/** 校验审计列表与详情共享的公开字段。 */
export const auditEventSchema = z
  .object({
    id: uuidSchema,
    category: z.enum(auditCategories),
    severity: z.enum(auditSeverities),
    action: z.string().min(1).max(128),
    summary: z.string().min(1).max(240),
    actor: auditActorSchema.nullable(),
    target: auditTargetSchema,
    requestId: z.string().max(128).nullable(),
    occurredAt: dateTimeSchema,
  })
  .strict();

/** 校验服务端实际应用的审计时间窗。 */
const appliedTimeWindowSchema = z
  .object({
    occurredFrom: dateTimeSchema,
    occurredTo: dateTimeSchema,
  })
  .strict();

/** 校验审计事件游标分页响应。 */
export const auditEventPageSchema = z
  .object({
    items: z.array(auditEventSchema).max(100),
    page: cursorPageSchema,
    appliedWindow: appliedTimeWindowSchema,
  })
  .strict();

/** 校验审计详情允许公开的用户安全快照。 */
const userSecuritySnapshotSchema = z
  .object({
    role: z.enum(userRoles).optional(),
    status: z.enum(userStatuses).optional(),
  })
  .strict();

/** 校验 action-specific allowlist，禁止原始 metadata 透传。 */
const auditDetailsSchema = z
  .object({
    actorRole: z.enum(userRoles).optional(),
    accountMasked: z.string().max(32).optional(),
    assignedRole: z.enum(["USER", "ADMIN"]).optional(),
    before: userSecuritySnapshotSchema.optional(),
    after: userSecuritySnapshotSchema.optional(),
    revokedFamilyCount: z.number().int().nonnegative().optional(),
  })
  .strict();

/** 校验脱敏审计详情响应。 */
export const auditEventDetailSchema = auditEventSchema
  .extend({
    details: auditDetailsSchema,
  })
  .strict();

/** 校验角色范围内的用户统计行。 */
const roleStatisticsSchema = z
  .object({
    role: z.enum(["USER", "ADMIN"]),
    total: z.number().int().nonnegative(),
    active: z.number().int().nonnegative(),
    disabled: z.number().int().nonnegative(),
    deleted: z.number().int().nonnegative(),
  })
  .strict();

/** 校验可管理用户统计响应。 */
export const manageableUserStatisticsSchema = z
  .object({
    generatedAt: dateTimeSchema,
    scope: z
      .array(z.enum(["USER", "ADMIN"]))
      .min(1)
      .max(2),
    total: z.number().int().nonnegative(),
    active: z.number().int().nonnegative(),
    disabled: z.number().int().nonnegative(),
    deleted: z.number().int().nonnegative(),
    loggedInLast30Days: z.number().int().nonnegative(),
    byRole: z.array(roleStatisticsSchema).min(1).max(2),
  })
  .strict();

/** 描述 Session family 列表请求。 */
export interface SessionFamilyListInput {
  cursor?: string;
  pageSize?: number;
}

/** 描述审计列表请求的冻结合同字段。 */
export interface AuditEventListInput {
  category?: AuditCategory;
  actorId?: string;
  targetId?: string;
  occurredFrom?: string;
  occurredTo?: string;
  includeRoutine?: boolean;
  cursor?: string;
  pageSize?: number;
}

/** 表示一个经过合同校验的 Session family。 */
export type SessionFamily = z.infer<typeof sessionFamilySchema>;

/** 表示经过合同校验的 Session family 分页。 */
export type SessionFamilyPage = z.infer<typeof sessionFamilyPageSchema>;

/** 表示退出其他 Session family 的聚合结果。 */
export type RevokeOtherSessionsResult = z.infer<typeof revokeOtherSessionsResultSchema>;

/** 表示一个经过合同校验的审计事件。 */
export type AuditEvent = z.infer<typeof auditEventSchema>;

/** 表示经过合同校验的审计事件分页。 */
export type AuditEventPage = z.infer<typeof auditEventPageSchema>;

/** 表示经过合同校验的脱敏审计详情。 */
export type AuditEventDetail = z.infer<typeof auditEventDetailSchema>;

/** 表示经过合同校验的可管理用户统计。 */
export type ManageableUserStatistics = z.infer<typeof manageableUserStatisticsSchema>;
