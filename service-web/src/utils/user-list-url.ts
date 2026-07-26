import type { UserListFilters } from "../types/access";

/** Describe dialog state represented in URL so browser navigation can close it. */
export type UserDialogKind = "create" | "edit" | "delete" | "reset-password";

/** Describe a valid user-management dialog target from current search parameters. */
export interface UserDialogState {
  kind: UserDialogKind;
  userId?: string;
}

/** Restrict accepted sort values to the frozen user-list contract. */
const allowedSorts = new Set<NonNullable<UserListFilters["sort"]>>([
  "createdAt",
  "updatedAt",
  "account",
  "displayName",
]);

/** Restrict accepted role filter values to listable management targets. */
const allowedRoles = new Set<NonNullable<UserListFilters["role"]>>(["USER", "ADMIN"]);

/** Restrict accepted lifecycle filters to fixed target contract states. */
const allowedStatuses = new Set<NonNullable<UserListFilters["status"]>>([
  "ACTIVE",
  "DISABLED",
  "DELETED",
]);

/** Validate an opaque user identifier enough to avoid opening a malformed dialog. */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

/** Parse URL filters and normalize omitted or invalid sort values to the list's stable default. */
export function readUserListFilters(search: URLSearchParams): UserListFilters {
  const requestedPageSize = Number.parseInt(search.get("pageSize") ?? "20", 10);
  const sortValue = search.get("sort");
  const orderValue = search.get("order");
  const roleValue = search.get("role");
  const statusValue = search.get("status");
  const query = search.get("q")?.trim();
  const cursor = search.get("cursor")?.trim();

  const sort = allowedSorts.has(sortValue as UserListFilters["sort"])
    ? (sortValue as UserListFilters["sort"])
    : "createdAt";

  return {
    ...(query === undefined || query.length === 0 ? {} : { q: query }),
    ...(roleValue === null || !allowedRoles.has(roleValue as NonNullable<UserListFilters["role"]>)
      ? {}
      : { role: roleValue as NonNullable<UserListFilters["role"]> }),
    ...(statusValue === null ||
    !allowedStatuses.has(statusValue as NonNullable<UserListFilters["status"]>)
      ? {}
      : { status: statusValue as NonNullable<UserListFilters["status"]> }),
    sort,
    order: orderValue === "asc" ? "asc" : "desc",
    ...(cursor === undefined || cursor.length === 0 ? {} : { cursor }),
    pageSize:
      Number.isSafeInteger(requestedPageSize) && requestedPageSize >= 1 && requestedPageSize <= 100
        ? requestedPageSize
        : 20,
  };
}

/** Parse URL dialog state and reject an edit/action target without a UUID. */
export function readUserDialogState(search: URLSearchParams): UserDialogState | undefined {
  const kind = search.get("dialog");

  if (kind === "create") {
    return { kind };
  }
  if (kind !== "edit" && kind !== "delete" && kind !== "reset-password") {
    return undefined;
  }

  const userId = search.get("userId");
  return userId !== null && isUuid(userId) ? { kind, userId } : undefined;
}
