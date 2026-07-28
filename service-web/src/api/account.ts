import { queryOptions } from "@tanstack/react-query";

import type { CurrentUser } from "../types/access";
import { authSession } from "./auth-session";
import { ApiError, requestJsonWithMetadata } from "./http";

/** 将当前用户资料与后续条件写入所需的强 `ETag` 配对。 */
export interface VersionedCurrentUser {
  user: CurrentUser;
  etag: string;
}

/** 描述个人资料允许修改的字段。 */
export interface UpdateCurrentProfileInput {
  displayName: string;
}

/** 描述只存在于改密 Dialog 内存的密码输入。 */
export interface ChangeCurrentPasswordInput {
  currentPassword: string;
  newPassword: string;
}

/** 读取当前用户资料，并保留响应中的强 `ETag`。 */
export async function getCurrentProfile(): Promise<VersionedCurrentUser> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<CurrentUser>("/api/v1/users/me", {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const etag = response.headers.get("ETag");

    if (etag === null) {
      throw new ApiError(500, "missing-etag");
    }

    return { user: response.data, etag };
  });
}

/** 在强 `ETag` 保护下修改本人显示名称。 */
export async function updateCurrentProfile(
  input: UpdateCurrentProfileInput,
  etag: string,
): Promise<VersionedCurrentUser> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<CurrentUser>("/api/v1/users/me/update", {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "If-Match": etag,
      },
      body: input,
    });
    const nextEtag = response.headers.get("ETag");

    if (nextEtag === null) {
      throw new ApiError(500, "missing-etag");
    }

    return { user: response.data, etag: nextEtag };
  });
}

/** 修改本人密码；成功后调用方必须立即清除全部本地会话状态。 */
export async function changeCurrentPassword(input: ChangeCurrentPasswordInput): Promise<void> {
  await authSession.withAccessToken(async (accessToken) => {
    await requestJsonWithMetadata<void>("/api/v1/users/me/password", {
      headers: { Authorization: `Bearer ${accessToken}` },
      body: input,
    });
  });
}

/** 构造包含强 `ETag` 的本人资料 Query。 */
export function currentProfileQueryOptions() {
  return queryOptions({
    queryKey: ["account", "profile"] as const,
    queryFn: getCurrentProfile,
    staleTime: 30_000,
  });
}
