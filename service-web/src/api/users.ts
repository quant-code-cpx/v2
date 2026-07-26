import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { authSession } from "./auth-session";
import { ApiError, requestJsonWithMetadata } from "./http";
import type {
  CreateUserInput,
  UpdateUserInput,
  User,
  UserListFilters,
  UserPage,
} from "../types/access";

/** Pair a user resource with its strong ETag for conditional mutations. */
export interface VersionedUser {
  user: User;
  etag: string;
}

/** Encode a URL-owned list filter object using only frozen contract parameters. */
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

/** Fetch one target-scoped user page with the current in-memory access token. */
export async function listUsers(filters: UserListFilters): Promise<UserPage> {
  const search = createUserListSearch(filters);

  return authSession.withAccessToken(async (accessToken) =>
    requestJsonWithMetadata<UserPage>(`/api/v1/users?${search}`, {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    }).then((response) => response.data),
  );
}

/** Fetch one user and preserve its required strong ETag for later writes. */
export async function getUser(userId: string): Promise<VersionedUser> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<User>(`/api/v1/users/${userId}`, {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const etag = response.headers.get("ETag");

    if (etag === null) {
      throw new ApiError(500, "missing-etag");
    }

    return { user: response.data, etag };
  });
}

/** Create a target-scoped user without retaining its initial password in any cache. */
export async function createUser(input: CreateUserInput): Promise<User> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<User>("/api/v1/users", {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}` },
      body: input,
    });

    return response.data;
  });
}

/** Update a user only when the caller supplies the exact detail ETag. */
export async function updateUser(
  userId: string,
  input: UpdateUserInput,
  etag: string,
): Promise<VersionedUser> {
  return authSession.withAccessToken(async (accessToken) => {
    const response = await requestJsonWithMetadata<User>(`/api/v1/users/${userId}`, {
      method: "PATCH",
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

/** Soft-delete a target-scoped user using the detail ETag. */
export async function deleteUser(userId: string, etag: string): Promise<void> {
  await authSession.withAccessToken(async (accessToken) => {
    await requestJsonWithMetadata<void>(`/api/v1/users/${userId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${accessToken}`, "If-Match": etag },
    });
  });
}

/** Replace a managed user's password without retaining it beyond this call. */
export async function resetUserPassword(
  userId: string,
  password: string,
  etag: string,
): Promise<void> {
  await authSession.withAccessToken(async (accessToken) => {
    await requestJsonWithMetadata<void>(`/api/v1/users/${userId}/password-reset`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}`, "If-Match": etag },
      body: { password },
    });
  });
}

/** Build a cacheable user-page query whose key mirrors shareable URL filters. */
export function userListQueryOptions(filters: UserListFilters) {
  return queryOptions({
    queryKey: ["users", "list", filters] as const,
    queryFn: () => listUsers(filters),
    placeholderData: keepPreviousData,
  });
}

/** Build a detail query that retains its ETag beside the resource. */
export function userDetailQueryOptions(userId: string) {
  return queryOptions({
    queryKey: ["users", "detail", userId] as const,
    queryFn: () => getUser(userId),
  });
}
