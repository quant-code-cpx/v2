import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { auditEventDetailSchema, auditEventPageSchema } from "../types/account-security";
import type {
  AuditEventDetail,
  AuditEventListInput,
  AuditEventPage,
} from "../types/account-security";
import { authSession } from "./auth-session";
import { requestJson } from "./http";

/** 查询脱敏审计事件，并严格拒绝原始 metadata 或额外字段。 */
export async function listAuditEvents(input: AuditEventListInput): Promise<AuditEventPage> {
  return authSession.withAccessToken(async (accessToken) => {
    const payload = await requestJson<unknown>("/api/v1/audit-events/list", {
      headers: { Authorization: `Bearer ${accessToken}` },
      body: input,
    });

    return auditEventPageSchema.parse(payload);
  });
}

/** 读取一个 action-specific allowlist 审计详情。 */
export async function getAuditEvent(eventId: string): Promise<AuditEventDetail> {
  return authSession.withAccessToken(async (accessToken) => {
    const payload = await requestJson<unknown>(`/api/v1/audit-events/${eventId}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });

    return auditEventDetailSchema.parse(payload);
  });
}

/** 构造 URL 筛选所有的审计列表 Query，并在背景刷新时保留已有表格。 */
export function auditEventListQueryOptions(input: AuditEventListInput) {
  return queryOptions({
    queryKey: ["audit-events", "list", input] as const,
    queryFn: () => listAuditEvents(input),
    placeholderData: keepPreviousData,
    staleTime: 15_000,
  });
}

/** 构造按 Drawer eventId 延迟启用的审计详情 Query。 */
export function auditEventDetailQueryOptions(eventId: string) {
  return queryOptions({
    queryKey: ["audit-events", "detail", eventId] as const,
    queryFn: () => getAuditEvent(eventId),
    staleTime: 30_000,
  });
}
