import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { manageableUserStatisticsSchema } from "../types/account-security";
import type { ManageableUserStatistics } from "../types/account-security";
import { authSession } from "./auth-session";
import { ApiError, requestJsonWithMetadata } from "./http";
import type {
  CreateUserInput,
  UpdateUserInput,
  User,
  UserListFilters,
  UserPage,
} from "../types/access";

/** 将用户资源与条件变更所需的强 `ETag` 配对。 */
export interface VersionedUser {
  user: User;
  etag: string;
}

/** 仅使用冻结合同参数编码 URL 管理的列表筛选对象。 */
function createUserListSearch(filters: UserListFilters): string {
  const search = new URLSearchParams({ pageSize: String(filters.pageSize) });

  search.set("sort", filters.sort);
  search.set("order", filters.order);

  if (filters.q !== undefined && filters.q.length > 0) {
    search.set("q", filters.q);
  }
  if (filters.role !== undefined) {
    search.set("role", filters.role);
  }
  if (filters.status !== undefined) {
    search.set("status", filters.status);
  }
  if (filters.cursor !== undefined) {
    search.set("cursor", filters.cursor);
  }

  return search.toString();
}

/** 使用当前内存 access token 获取一页目标范围内的用户。 */
export async function listUsers(filters: UserListFilters): Promise<UserPage> {
  const search = createUserListSearch(filters);

  return authSession.withAccessToken(async (accessToken) =>
    requestJsonWithMetadata<UserPage>(`/api/v1/users/list?${search}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    }).then((response) => response.data),
  );
}

/** 获取一个用户，并保留后续变更所需的强 `ETag`。 */
export async function getUser(userId: string): Promise<VersionedUser> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<User>(`/api/v1/users/${userId}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const etag = response.headers.get("ETag");

    if (etag === null) {
      throw new ApiError(500, "missing-etag");
    }

    return { user: response.data, etag };
  });
}

/** 创建目标范围内的用户，且不在任何缓存中保留初始密码。 */
export async function createUser(input: CreateUserInput): Promise<User> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<User>("/api/v1/users", {
      headers: { Authorization: `Bearer ${accessToken}` },
      body: input,
    });

    return response.data;
  });
}

/** 仅在调用方提供精确详情 `ETag` 时更新用户。 */
export async function updateUser(
  userId: string,
  input: UpdateUserInput,
  etag: string,
): Promise<VersionedUser> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<User>(`/api/v1/users/${userId}/update`, {
      headers: { Authorization: `Bearer ${accessToken}`, "If-Match": etag },
      body: input,
    });
    const nextEtag = response.headers.get("ETag");

    if (nextEtag === null) {
      throw new ApiError(500, "missing-etag");
    }

    return { user: response.data, etag: nextEtag };
  });
}

/** 使用详情 `ETag` 软删除目标范围内的用户。 */
export async function deleteUser(userId: string, etag: string): Promise<void> {
  await authSession.withAccessToken(async (accessToken) => {
    await requestJsonWithMetadata<void>(`/api/v1/users/${userId}/delete`, {
      headers: { Authorization: `Bearer ${accessToken}`, "If-Match": etag },
    });
  });
}

/** 替换可管理用户的密码，且不在本次调用后保留密码。 */
export async function resetUserPassword(
  userId: string,
  password: string,
  etag: string,
): Promise<void> {
  await authSession.withAccessToken(async (accessToken) => {
    await requestJsonWithMetadata<void>(`/api/v1/users/${userId}/password-reset`, {
      headers: { Authorization: `Bearer ${accessToken}`, "If-Match": etag },
      body: { password },
    });
  });
}

/** 查询调用方角色范围内的用户聚合统计，不返回任何用户明细。 */
export async function getManageableUserStatistics(): Promise<ManageableUserStatistics> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<unknown>("/api/v1/users/statistics", {
      headers: { Authorization: `Bearer ${accessToken}` },
    });

    return manageableUserStatisticsSchema.parse(response.data);
  });
}

/** 构造可缓存的用户页查询，使查询键与可分享 URL 筛选条件一致。 */
export function userListQueryOptions(filters: UserListFilters) {
  return queryOptions({
    queryKey: ["users", "list", filters] as const,
    queryFn: () => listUsers(filters),
    placeholderData: keepPreviousData,
  });
}

/** 构造详情查询，并在资源旁保留其 `ETag`。 */
export function userDetailQueryOptions(userId: string) {
  return queryOptions({
    queryKey: ["users", "detail", userId] as const,
    queryFn: () => getUser(userId),
  });
}

/** 构造角色范围用户统计 Query。 */
export function manageableUserStatisticsQueryOptions() {
  return queryOptions({
    queryKey: ["users", "statistics"] as const,
    queryFn: getManageableUserStatistics,
    staleTime: 60_000,
  });
}
