import { isUuid } from "../../AuditEventsView/utils/audit-event-url";

/** 描述个人中心 URL 持有的聚焦安全动作。 */
export type AccountDialogState =
  | { kind: "change-password" }
  | { kind: "revoke-others" }
  | { kind: "revoke-session"; familyId: string };

/** 从 URL 读取一个允许的个人中心 Dialog 状态。 */
export function parseAccountDialogState(
  searchParameters: URLSearchParams,
): AccountDialogState | undefined {
  const dialog = searchParameters.get("dialog");

  if (dialog === "change-password") {
    return { kind: "change-password" };
  }
  if (dialog === "revoke-others") {
    return { kind: "revoke-others" };
  }
  if (dialog === "revoke-session") {
    const familyId = searchParameters.get("familyId");

    return familyId !== null && isUuid(familyId) ? { kind: "revoke-session", familyId } : undefined;
  }

  return undefined;
}

/** 序列化一个可由浏览器返回关闭的个人中心 Dialog。 */
export function serializeAccountDialogState(
  state: AccountDialogState | undefined,
): URLSearchParams {
  const searchParameters = new URLSearchParams();

  if (state === undefined) {
    return searchParameters;
  }

  searchParameters.set("dialog", state.kind);
  if (state.kind === "revoke-session") {
    searchParameters.set("familyId", state.familyId);
  }

  return searchParameters;
}
