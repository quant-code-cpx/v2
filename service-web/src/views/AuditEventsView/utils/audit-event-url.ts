import { auditCategories } from "../../../types/account-security";
import type { AuditCategory, AuditEventListInput } from "../../../types/account-security";

/** 枚举审计页支持的固定回看范围。 */
export const auditRanges = ["24h", "7d", "30d", "90d"] as const;

/** 表示一个固定审计回看范围。 */
export type AuditRange = (typeof auditRanges)[number];

/** 描述由浏览器 URL 持有的审计筛选与选中详情。 */
export interface AuditUrlState {
  category?: AuditCategory;
  range: AuditRange;
  actorId?: string;
  targetId?: string;
  includeRoutine: boolean;
  cursor?: string;
  eventId?: string;
}

/** 检查字符串是否为可交给合同 0017 的 UUID。 */
export function isUuid(value: string): boolean {
  return /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/iu.test(value);
}

/** 从 URL 读取并规范化审计筛选，丢弃未知枚举和无效标识。 */
export function parseAuditUrlState(searchParameters: URLSearchParams): AuditUrlState {
  const categoryValue = searchParameters.get("category");
  const rangeValue = searchParameters.get("range");
  const actorIdValue = searchParameters.get("actorId")?.trim();
  const targetIdValue = searchParameters.get("targetId")?.trim();
  const cursorValue = searchParameters.get("cursor")?.trim();
  const eventIdValue = searchParameters.get("eventId")?.trim();

  return {
    ...(auditCategories.includes(categoryValue as AuditCategory)
      ? { category: categoryValue as AuditCategory }
      : {}),
    range: auditRanges.includes(rangeValue as AuditRange) ? (rangeValue as AuditRange) : "7d",
    ...(actorIdValue !== undefined && isUuid(actorIdValue) ? { actorId: actorIdValue } : {}),
    ...(targetIdValue !== undefined && isUuid(targetIdValue) ? { targetId: targetIdValue } : {}),
    includeRoutine: searchParameters.get("includeRoutine") === "true",
    ...(cursorValue !== undefined && cursorValue.length <= 512 ? { cursor: cursorValue } : {}),
    ...(eventIdValue !== undefined && isUuid(eventIdValue) ? { eventId: eventIdValue } : {}),
  };
}

/** 将规范审计状态写回可分享 URL，默认值保持省略。 */
export function serializeAuditUrlState(state: AuditUrlState): URLSearchParams {
  const searchParameters = new URLSearchParams();

  if (state.category !== undefined) {
    searchParameters.set("category", state.category);
  }
  if (state.range !== "7d") {
    searchParameters.set("range", state.range);
  }
  if (state.actorId !== undefined) {
    searchParameters.set("actorId", state.actorId);
  }
  if (state.targetId !== undefined) {
    searchParameters.set("targetId", state.targetId);
  }
  if (state.includeRoutine) {
    searchParameters.set("includeRoutine", "true");
  }
  if (state.cursor !== undefined) {
    searchParameters.set("cursor", state.cursor);
  }
  if (state.eventId !== undefined) {
    searchParameters.set("eventId", state.eventId);
  }

  return searchParameters;
}

/** 把固定 URL 范围转换为本次请求的精确 ISO 时间窗。 */
export function toAuditListInput(
  state: AuditUrlState,
  requestTime = new Date(),
  pageSize = 20,
): AuditEventListInput {
  const rangeMilliseconds: Record<AuditRange, number> = {
    "24h": 24 * 60 * 60 * 1_000,
    "7d": 7 * 24 * 60 * 60 * 1_000,
    "30d": 30 * 24 * 60 * 60 * 1_000,
    "90d": 90 * 24 * 60 * 60 * 1_000,
  };
  const occurredTo = requestTime.toISOString();
  const occurredFrom = new Date(
    requestTime.getTime() - rangeMilliseconds[state.range],
  ).toISOString();

  return {
    ...(state.category === undefined ? {} : { category: state.category }),
    ...(state.actorId === undefined ? {} : { actorId: state.actorId }),
    ...(state.targetId === undefined ? {} : { targetId: state.targetId }),
    occurredFrom,
    occurredTo,
    includeRoutine: state.includeRoutine,
    ...(state.cursor === undefined ? {} : { cursor: state.cursor }),
    pageSize,
  };
}

/** 返回审计范围的中文标签。 */
export function auditRangeLabel(range: AuditRange): string {
  const labels: Record<AuditRange, string> = {
    "24h": "最近 24 小时",
    "7d": "最近 7 天",
    "30d": "最近 30 天",
    "90d": "最近 90 天",
  };

  return labels[range];
}
