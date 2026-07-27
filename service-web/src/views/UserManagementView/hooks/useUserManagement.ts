import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import type { ChangeEvent } from "react";

import { userListQueryOptions } from "../../../api/users";
import { useAuth } from "../../../components/AuthProvider";
import type { UserListFilters } from "../../../types/access";
import { readUserDialogState, readUserListFilters } from "../../../utils/user-list-url";

/** 描述一个桌面筛选控件发起的 URL 字段更新。 */
type FilterUpdate = Partial<UserListFilters> & { q?: string; resetCursor?: boolean };

/** 空筛选值删除对应 URL 字段，其余值写入 URL。 */
function writeOptionalSearchParam(
  search: URLSearchParams,
  key: string,
  value: string | undefined,
): void {
  if (value === undefined || value.length === 0) {
    search.delete(key);
    return;
  }

  search.set(key, value);
}

/** 管理用户页 URL 状态、远程查询、权限派生与页面动作。 */
export function useUserManagement() {
  const { user, hasPermission } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => readUserListFilters(searchParams), [searchParams]);
  const dialogState = useMemo(() => readUserDialogState(searchParams), [searchParams]);
  const listQuery = useQuery(userListQueryOptions(filters));
  const { refetch } = listQuery;

  /** 更新 URL 筛选，并在未明确指定 cursor 时回到首个游标页。 */
  const updateFilters = useCallback(
    (update: FilterUpdate) => {
      const nextSearch = new URLSearchParams(searchParams);
      const shouldResetCursor = update.resetCursor ?? true;

      if ("q" in update) {
        writeOptionalSearchParam(nextSearch, "q", update.q);
      }
      if ("role" in update) {
        writeOptionalSearchParam(nextSearch, "role", update.role);
      }
      if ("status" in update) {
        writeOptionalSearchParam(nextSearch, "status", update.status);
      }
      if (update.sort !== undefined) {
        nextSearch.set("sort", update.sort);
      }
      if (update.order !== undefined) {
        nextSearch.set("order", update.order);
      }
      if (update.pageSize !== undefined) {
        nextSearch.set("pageSize", String(update.pageSize));
      }
      if (update.cursor !== undefined) {
        nextSearch.set("cursor", update.cursor);
      } else if (shouldResetCursor) {
        nextSearch.delete("cursor");
      }

      setSearchParams(nextSearch);
    },
    [searchParams, setSearchParams],
  );

  /** 通过 URL 打开 Dialog，使浏览器后退可退出聚焦任务。 */
  const openDialog = useCallback(
    (kind: "create" | "edit" | "delete" | "reset-password", userId?: string) => {
      const nextSearch = new URLSearchParams(searchParams);
      nextSearch.set("dialog", kind);
      if (userId === undefined) {
        nextSearch.delete("userId");
      } else {
        nextSearch.set("userId", userId);
      }

      setSearchParams(nextSearch);
    },
    [searchParams, setSearchParams],
  );

  /** 打开创建用户 Dialog。 */
  const handleCreate = useCallback(() => {
    openDialog("create");
  }, [openDialog]);

  /** 打开指定用户的编辑 Dialog。 */
  const handleEdit = useCallback(
    (userId: string) => {
      openDialog("edit", userId);
    },
    [openDialog],
  );

  /** 打开指定用户的密码重置 Dialog。 */
  const handleResetPassword = useCallback(
    (userId: string) => {
      openDialog("reset-password", userId);
    },
    [openDialog],
  );

  /** 打开指定用户的删除确认 Dialog。 */
  const handleDelete = useCallback(
    (userId: string) => {
      openDialog("delete", userId);
    },
    [openDialog],
  );

  /** 只移除 Dialog URL 状态，保留可分享筛选与 cursor。 */
  const closeDialog = useCallback(() => {
    const nextSearch = new URLSearchParams(searchParams);
    nextSearch.delete("dialog");
    nextSearch.delete("userId");
    setSearchParams(nextSearch);
  }, [searchParams, setSearchParams]);

  /** 重置列表筛选与 cursor，不保留历史 Dialog 状态。 */
  const resetFilters = useCallback(() => {
    const nextSearch = new URLSearchParams(searchParams);
    for (const key of ["q", "role", "status", "sort", "order", "cursor", "pageSize"]) {
      nextSearch.delete(key);
    }
    setSearchParams(nextSearch);
  }, [searchParams, setSearchParams]);

  /** 将账号或姓名搜索直接同步到 URL。 */
  const handleSearchChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      updateFilters({ q: event.target.value });
    },
    [updateFilters],
  );

  /** 更新契约允许的固定角色筛选。 */
  const handleRoleChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      updateFilters({ role: value === "ADMIN" ? "ADMIN" : value === "USER" ? "USER" : undefined });
    },
    [updateFilters],
  );

  /** 更新生命周期筛选，是否可见 DELETED 仍由服务端权限决定。 */
  const handleStatusChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      updateFilters({
        status:
          value === "ACTIVE" || value === "DISABLED" || value === "DELETED" ? value : undefined,
      });
    },
    [updateFilters],
  );

  /** 在可见表格字段的升序与降序之间切换。 */
  const handleTableSort = useCallback(
    (sort: NonNullable<UserListFilters["sort"]>) => {
      const primaryOrder = sort === "account" || sort === "displayName" ? "asc" : "desc";
      const isCurrentSort = filters.sort === sort;
      const order = isCurrentSort ? (filters.order === "asc" ? "desc" : "asc") : primaryOrder;
      updateFilters({ sort, order });
    },
    [filters.order, filters.sort, updateFilters],
  );

  /** 使用服务端 opaque cursor 前往下一页。 */
  const goToNextPage = useCallback(() => {
    const nextCursor = listQuery.data?.page.nextCursor;
    if (nextCursor !== null && nextCursor !== undefined) {
      updateFilters({ cursor: nextCursor, resetCursor: false });
    }
  }, [listQuery.data?.page.nextCursor, updateFilters]);

  /** 保留当前筛选并返回首个 cursor 页。 */
  const goToFirstPage = useCallback(() => {
    const nextSearch = new URLSearchParams(searchParams);
    nextSearch.delete("cursor");
    setSearchParams(nextSearch);
  }, [searchParams, setSearchParams]);

  /** 重试或刷新当前查询，不改变 URL 状态。 */
  const refreshList = useCallback(() => {
    void refetch();
  }, [refetch]);

  return {
    user,
    filters,
    dialogState,
    listQuery,
    canCreate: hasPermission("users:create"),
    canUpdate: hasPermission("users:update"),
    canResetPassword: hasPermission("users:reset-password"),
    canDelete: hasPermission("users:delete"),
    canManageAdmins: hasPermission("admins:manage") || hasPermission("admins:create"),
    canSeeDeleted: user?.role === "SUPER_ADMIN",
    handleSearchChange,
    handleRoleChange,
    handleStatusChange,
    handleTableSort,
    resetFilters,
    refreshList,
    goToFirstPage,
    goToNextPage,
    handleCreate,
    handleEdit,
    handleResetPassword,
    handleDelete,
    closeDialog,
  };
}

/** 暴露页面私有组件可复用的 Hook 返回类型。 */
export type UserManagementModel = ReturnType<typeof useUserManagement>;
