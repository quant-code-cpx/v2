import { queryOptions } from "@tanstack/react-query";

import {
  revokeOtherSessionsResultSchema,
  sessionFamilyPageSchema,
} from "../types/account-security";
import type {
  RevokeOtherSessionsResult,
  SessionFamilyListInput,
  SessionFamilyPage,
} from "../types/account-security";
import { authSession } from "./auth-session";
import { requestJson } from "./http";

/** 返回本人活动 Session family，并拒绝任何未满足合同 0017 的响应。 */
export async function listSessionFamilies(
  input: SessionFamilyListInput = {},
): Promise<SessionFamilyPage> {
  return authSession.withAccessToken(async (accessToken) => {
    const payload = await requestJson<unknown>("/api/v1/auth/sessions/list", {
      headers: { Authorization: `Bearer ${accessToken}` },
      body: input,
    });

    return sessionFamilyPageSchema.parse(payload);
  });
}

/** 撤销一个属于本人的 Session family；未知或越权目标由服务端统一返回 404。 */
export async function revokeSessionFamily(familyId: string): Promise<void> {
  await authSession.withAccessToken(async (accessToken) => {
    await requestJson<void>(`/api/v1/auth/sessions/${familyId}/revoke`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
  });
}

/** 撤销除当前 family 外的全部本人活动 Session family。 */
export async function revokeOtherSessionFamilies(): Promise<RevokeOtherSessionsResult> {
  return authSession.withAccessToken(async (accessToken) => {
    const payload = await requestJson<unknown>("/api/v1/auth/sessions/revoke-others", {
      headers: { Authorization: `Bearer ${accessToken}` },
    });

    return revokeOtherSessionsResultSchema.parse(payload);
  });
}

/** 构造本人 Session family Query，查询键完整反映游标输入。 */
export function sessionFamiliesQueryOptions(input: SessionFamilyListInput = {}) {
  return queryOptions({
    queryKey: ["auth", "session-families", input] as const,
    queryFn: () => listSessionFamilies(input),
    staleTime: 30_000,
  });
}
