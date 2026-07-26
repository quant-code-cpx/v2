import {
  AddOutlined as AddOutlinedIcon,
  DeleteOutlineOutlined as DeleteOutlineOutlinedIcon,
  EditOutlined as EditOutlinedIcon,
  KeyOutlined as KeyOutlinedIcon,
  RefreshOutlined as RefreshOutlinedIcon,
  RestartAltOutlined as RestartAltOutlinedIcon,
  SearchOffOutlined as SearchOffOutlinedIcon,
} from "@mui/icons-material";
import {
  Alert,
  Avatar,
  Box,
  Breadcrumbs,
  Button,
  Card,
  CardContent,
  Chip,
  IconButton,
  MenuItem,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TableSortLabel,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import type { ChangeEvent } from "react";

import { userListQueryOptions } from "../api/users";
import { useAuth } from "../components/AuthProvider";
import { UserActionDialog } from "../components/UserActionDialog";
import { UserEditorDialog } from "../components/UserEditorDialog";
import { brandColors } from "../styles/design-tokens";
import type { User, UserListFilters } from "../types/access";
import { readUserDialogState, readUserListFilters } from "../utils/user-list-url";
import { userRoleLabel, userStatusLabel } from "../utils/user-presentation";

/** Format management timestamps in the browser's Chinese locale without adding seconds noise. */
const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** Format the last successful query time without implying backend data freshness. */
const updatedTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** Describe a set of URL filter updates initiated by one desktop control. */
type FilterUpdate = Partial<UserListFilters> & { q?: string; resetCursor?: boolean };

/** Return a clear URL filter field when its control has no active value. */
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

/** Render a standard table-header sort control while retaining the URL-owned list state. */
function SortableTableHeader({
  label,
  sort,
  activeSort,
  order,
  onSort,
  width,
}: {
  label: string;
  sort: NonNullable<UserListFilters["sort"]>;
  activeSort: UserListFilters["sort"];
  order: UserListFilters["order"];
  onSort: (sort: NonNullable<UserListFilters["sort"]>) => void;
  width: string;
}) {
  const isActive = activeSort === sort;
  const currentOrderLabel = order === "asc" ? "升序" : "降序";
  const currentOrder = order;

  return (
    <TableCell
      width={width}
      sortDirection={isActive ? currentOrder : false}
      sx={{ px: 1.5, whiteSpace: "nowrap" }}
    >
      <TableSortLabel
        active={isActive}
        direction={isActive ? currentOrder : "asc"}
        onClick={() => onSort(sort)}
        aria-label={`按${label}排序，${isActive ? `当前${currentOrderLabel}` : "当前未排序"}`}
      >
        {label}
      </TableSortLabel>
    </TableCell>
  );
}

/** Render one role/status-aware table row with permission-filtered management actions. */
function UserTableRow({
  user,
  isSelf,
  canUpdate,
  canResetPassword,
  canDelete,
  onEdit,
  onResetPassword,
  onDelete,
}: {
  user: User;
  isSelf: boolean;
  canUpdate: boolean;
  canResetPassword: boolean;
  canDelete: boolean;
  onEdit: (userId: string) => void;
  onResetPassword: (userId: string) => void;
  onDelete: (userId: string) => void;
}) {
  const isDeleted = user.status === "DELETED";
  const isReadOnly = isDeleted || isSelf;

  /** Open edit dialog for this target without putting account data into the URL. */
  const handleEdit = useCallback(() => {
    onEdit(user.id);
  }, [onEdit, user.id]);

  /** Open reset dialog for this target without storing a password in route state. */
  const handleResetPassword = useCallback(() => {
    onResetPassword(user.id);
  }, [onResetPassword, user.id]);

  /** Open explicit delete confirmation for this target. */
  const handleDelete = useCallback(() => {
    onDelete(user.id);
  }, [onDelete, user.id]);

  return (
    <TableRow hover>
      <TableCell>
        <Stack direction="row" spacing={1.5} alignItems="center">
          <Avatar
            sx={{
              width: 36,
              height: 36,
              bgcolor: isSelf ? "primary.main" : "grey.200",
              color: isSelf ? "primary.contrastText" : "text.secondary",
              fontSize: 14,
              fontWeight: 700,
            }}
          >
            {user.displayName.trim().slice(0, 1) || "A"}
          </Avatar>
          <Box>
            <Stack direction="row" spacing={1} alignItems="center">
              <Typography fontWeight={700}>{user.displayName}</Typography>
              {isSelf ? (
                <Chip label="当前账号" size="small" color="primary" variant="outlined" />
              ) : null}
            </Stack>
            <Typography variant="caption" color="text.secondary">
              {user.account}
            </Typography>
          </Box>
        </Stack>
      </TableCell>
      <TableCell>{userRoleLabel(user.role)}</TableCell>
      <TableCell>
        <Chip
          label={userStatusLabel(user.status)}
          size="small"
          color={
            user.status === "ACTIVE" ? "success" : user.status === "DISABLED" ? "default" : "error"
          }
          variant={user.status === "ACTIVE" ? "outlined" : "filled"}
        />
      </TableCell>
      <TableCell sx={{ px: 1.5, whiteSpace: "nowrap" }}>
        {user.lastLoginAt === null
          ? "未登录"
          : dateTimeFormatter.format(new Date(user.lastLoginAt))}
      </TableCell>
      <TableCell sx={{ px: 1.5, whiteSpace: "nowrap" }}>
        {dateTimeFormatter.format(new Date(user.updatedAt))}
      </TableCell>
      <TableCell sx={{ px: 1.5, whiteSpace: "nowrap" }}>
        {dateTimeFormatter.format(new Date(user.createdAt))}
      </TableCell>
      <TableCell align="right">
        {isSelf ? (
          <Typography variant="caption" color="text.secondary">
            本人账号
          </Typography>
        ) : (
          <Stack direction="row" justifyContent="flex-end" spacing={0.5}>
            {!isReadOnly && canUpdate ? (
              <Tooltip title="编辑用户">
                <IconButton aria-label="编辑用户" onClick={handleEdit}>
                  <EditOutlinedIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            ) : null}
            {!isReadOnly && canResetPassword ? (
              <Tooltip title="重置密码">
                <IconButton aria-label="重置密码" onClick={handleResetPassword}>
                  <KeyOutlinedIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            ) : null}
            {!isReadOnly && canDelete ? (
              <Tooltip title="删除用户">
                <IconButton aria-label="删除用户" color="error" onClick={handleDelete}>
                  <DeleteOutlineOutlinedIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            ) : null}
          </Stack>
        )}
      </TableCell>
    </TableRow>
  );
}

/** Render stable table-row geometry while the target-scoped list is loading. */
function UserTableSkeleton() {
  /** Render four rows matching standard user table density. */
  const skeletonRows = Array.from({ length: 4 }, (_, index) => index);

  return (
    <TableBody>
      {/* Render one stable skeleton row for each expected first-page result. */}
      {skeletonRows.map((index) => (
        <TableRow key={index}>
          <TableCell>
            <Skeleton variant="text" width="72%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="60%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="rounded" width={64} height={24} />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="84%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="84%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="84%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="rounded" width={104} height={32} sx={{ ml: "auto" }} />
          </TableCell>
        </TableRow>
      ))}
    </TableBody>
  );
}

/** Render protected, URL-driven user administration with Dialog-based mutations. */
export function UserManagementView() {
  const { user, hasPermission } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => readUserListFilters(searchParams), [searchParams]);
  const dialogState = useMemo(() => readUserDialogState(searchParams), [searchParams]);
  const listQuery = useQuery(userListQueryOptions(filters));

  /** Update only list controls in URL and reset pagination unless caller sets a new cursor. */
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

  /** Open a Dialog through URL state so browser back can exit focused work. */
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

  /** Remove only Dialog state while retaining shareable filters and cursor. */
  const closeDialog = useCallback(() => {
    const nextSearch = new URLSearchParams(searchParams);
    nextSearch.delete("dialog");
    nextSearch.delete("userId");
    setSearchParams(nextSearch);
  }, [searchParams, setSearchParams]);

  /** Reset all list filters while preserving no previous cursor or modal state. */
  const resetFilters = useCallback(() => {
    const nextSearch = new URLSearchParams(searchParams);
    ["q", "role", "status", "sort", "order", "cursor", "pageSize"].forEach((key) => {
      nextSearch.delete(key);
    });
    setSearchParams(nextSearch);
  }, [searchParams, setSearchParams]);

  /** Update account/name search directly in URL; query cache owns remote data. */
  const handleSearchChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      updateFilters({ q: event.target.value });
    },
    [updateFilters],
  );

  /** Update a fixed role filter without exposing roles unsupported by the contract. */
  const handleRoleChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      updateFilters({ role: value === "ADMIN" ? "ADMIN" : value === "USER" ? "USER" : undefined });
    },
    [updateFilters],
  );

  /** Update lifecycle filter and preserve DELETED visibility as a server decision. */
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

  /** Cycle one visible table field between ascending and descending order. */
  const handleTableSort = useCallback(
    (sort: NonNullable<UserListFilters["sort"]>) => {
      const primaryOrder = sort === "account" || sort === "displayName" ? "asc" : "desc";
      const isCurrentSort = filters.sort === sort;
      const order = isCurrentSort ? (filters.order === "asc" ? "desc" : "asc") : primaryOrder;
      updateFilters({ sort, order });
    },
    [filters.order, filters.sort, updateFilters],
  );

  /** Advance through a server-provided opaque cursor without inventing page numbers. */
  const goToNextPage = useCallback(() => {
    const nextCursor = listQuery.data?.page.nextCursor;
    if (nextCursor !== null && nextCursor !== undefined) {
      updateFilters({ cursor: nextCursor, resetCursor: false });
    }
  }, [listQuery.data?.page.nextCursor, updateFilters]);

  /** Return to the first cursor page while retaining all current filters. */
  const goToFirstPage = useCallback(() => {
    const nextSearch = new URLSearchParams(searchParams);
    nextSearch.delete("cursor");
    setSearchParams(nextSearch);
  }, [searchParams, setSearchParams]);

  /** Open edit route state for one target resource. */
  const handleEdit = useCallback(
    (userId: string) => {
      openDialog("edit", userId);
    },
    [openDialog],
  );

  /** Open password reset route state for one target resource. */
  const handleResetPassword = useCallback(
    (userId: string) => {
      openDialog("reset-password", userId);
    },
    [openDialog],
  );

  /** Open delete route state for one target resource. */
  const handleDelete = useCallback(
    (userId: string) => {
      openDialog("delete", userId);
    },
    [openDialog],
  );

  /** Retry a failed list request without changing URL-owned filters. */
  const retryList = useCallback(() => {
    void listQuery.refetch();
  }, [listQuery]);

  /** Refresh the current cursor page while retaining every URL-owned filter. */
  const refreshList = useCallback(() => {
    void listQuery.refetch();
  }, [listQuery]);

  if (user === undefined) {
    return <UserManagementLoading />;
  }

  const canCreate = hasPermission("users:create");
  const canUpdate = hasPermission("users:update");
  const canResetPassword = hasPermission("users:reset-password");
  const canDelete = hasPermission("users:delete");
  const canManageAdmins = hasPermission("admins:manage") || hasPermission("admins:create");
  const canSeeDeleted = user.role === "SUPER_ADMIN";

  return (
    <Stack spacing={3}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Breadcrumbs aria-label="当前位置" separator="/" sx={{ mb: 0.75 }}>
            <Typography variant="body2" color="text.secondary">
              系统管理
            </Typography>
            <Typography variant="body2" color="text.primary">
              用户管理
            </Typography>
          </Breadcrumbs>
          <Typography component="h1" variant="h3">
            用户管理
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            查询、创建和维护平台账号；操作范围由服务端权限决定。
          </Typography>
        </Box>
        {canCreate ? (
          <Button
            variant="contained"
            size="large"
            startIcon={<AddOutlinedIcon />}
            onClick={() => openDialog("create")}
          >
            新建用户
          </Button>
        ) : null}
      </Stack>

      <Card component="section" aria-label="用户筛选">
        <CardContent>
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: "minmax(240px, 1fr) repeat(2, minmax(136px, 160px)) auto",
              gap: 1.5,
              alignItems: "center",
            }}
          >
            <TextField
              label="搜索"
              placeholder="账号或姓名"
              value={filters.q ?? ""}
              onChange={handleSearchChange}
            />
            <TextField select label="角色" value={filters.role ?? ""} onChange={handleRoleChange}>
              <MenuItem value="">全部角色</MenuItem>
              <MenuItem value="USER">普通用户</MenuItem>
              {canManageAdmins ? <MenuItem value="ADMIN">管理员</MenuItem> : null}
            </TextField>
            <TextField
              select
              label="状态"
              value={filters.status ?? ""}
              onChange={handleStatusChange}
            >
              <MenuItem value="">全部状态</MenuItem>
              <MenuItem value="ACTIVE">启用</MenuItem>
              <MenuItem value="DISABLED">已禁用</MenuItem>
              {canSeeDeleted ? <MenuItem value="DELETED">已删除</MenuItem> : null}
            </TextField>
            <Stack direction="row" spacing={0.5} alignItems="center" justifyContent="flex-end">
              <Tooltip title="刷新当前用户列表">
                <span>
                  <IconButton
                    aria-label="刷新用户列表"
                    onClick={refreshList}
                    disabled={listQuery.isFetching}
                  >
                    <RefreshOutlinedIcon />
                  </IconButton>
                </span>
              </Tooltip>
              <Button startIcon={<RestartAltOutlinedIcon />} onClick={resetFilters}>
                重置
              </Button>
            </Stack>
          </Box>
        </CardContent>
      </Card>

      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ px: 0.5 }}>
        <Stack direction="row" spacing={1} alignItems="baseline">
          <Typography fontWeight={700}>全部用户</Typography>
          <Typography variant="body2" color="text.secondary">
            {listQuery.data === undefined
              ? "正在加载"
              : `本页 ${listQuery.data.items.length} 个账号`}
          </Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary">
          {listQuery.dataUpdatedAt === 0
            ? "尚未更新"
            : `更新于 ${updatedTimeFormatter.format(new Date(listQuery.dataUpdatedAt))}`}
        </Typography>
      </Stack>

      <Card>
        <CardContent sx={{ p: 0 }}>
          {listQuery.isError ? (
            <Box sx={{ p: 3 }}>
              <Alert
                severity="error"
                action={
                  <Button color="inherit" size="small" onClick={retryList}>
                    重试
                  </Button>
                }
              >
                用户列表暂时不可用，请稍后重试。
              </Alert>
            </Box>
          ) : null}
          {listQuery.isError && listQuery.data === undefined ? null : (
            <TableContainer>
              <Table
                aria-label="用户列表"
                sx={{
                  minWidth: 900,
                  tableLayout: "fixed",
                  "& .MuiTableCell-root": { px: 2 },
                }}
              >
                <TableHead>
                  <TableRow>
                    <SortableTableHeader
                      label="用户"
                      sort="displayName"
                      activeSort={filters.sort}
                      order={filters.order}
                      onSort={handleTableSort}
                      width="28%"
                    />
                    <TableCell width="10%">角色</TableCell>
                    <TableCell width="10%">状态</TableCell>
                    <TableCell width="14%" sx={{ px: 1.5, whiteSpace: "nowrap" }}>
                      最近登录
                    </TableCell>
                    <SortableTableHeader
                      label="更新时间"
                      sort="updatedAt"
                      activeSort={filters.sort}
                      order={filters.order}
                      onSort={handleTableSort}
                      width="14%"
                    />
                    <SortableTableHeader
                      label="创建时间"
                      sort="createdAt"
                      activeSort={filters.sort}
                      order={filters.order}
                      onSort={handleTableSort}
                      width="14%"
                    />
                    <TableCell width="10%" align="right">
                      操作
                    </TableCell>
                  </TableRow>
                </TableHead>
                {listQuery.isPending || listQuery.data === undefined ? <UserTableSkeleton /> : null}
                {listQuery.data !== undefined && listQuery.data.items.length === 0 ? (
                  <TableBody>
                    <TableRow>
                      <TableCell colSpan={7} align="center" sx={{ py: 8 }}>
                        <Stack spacing={1.5} alignItems="center">
                          <Box
                            sx={{
                              width: 48,
                              height: 48,
                              display: "grid",
                              placeItems: "center",
                              borderRadius: 1.5,
                              bgcolor: brandColors.primaryLighter,
                              color: "primary.main",
                            }}
                          >
                            <SearchOffOutlinedIcon />
                          </Box>
                          <Typography fontWeight={700}>没有匹配用户</Typography>
                          <Typography variant="body2" color="text.secondary">
                            调整筛选条件后重试。
                          </Typography>
                          <Button size="small" onClick={resetFilters}>
                            重置筛选
                          </Button>
                        </Stack>
                      </TableCell>
                    </TableRow>
                  </TableBody>
                ) : null}
                {listQuery.data !== undefined && listQuery.data.items.length > 0 ? (
                  <TableBody>
                    {/* Keep table rows isolated so interaction callbacks do not rerender filters. */}
                    {listQuery.data.items.map((managedUser) => (
                      <UserTableRow
                        key={managedUser.id}
                        user={managedUser}
                        isSelf={managedUser.id === user.id}
                        canUpdate={canUpdate}
                        canResetPassword={canResetPassword}
                        canDelete={canDelete}
                        onEdit={handleEdit}
                        onResetPassword={handleResetPassword}
                        onDelete={handleDelete}
                      />
                    ))}
                  </TableBody>
                ) : null}
              </Table>
            </TableContainer>
          )}
          <Stack
            direction="row"
            justifyContent="space-between"
            alignItems="center"
            sx={{ px: 3, py: 2 }}
          >
            <Typography variant="body2" color="text.secondary">
              {listQuery.data === undefined
                ? "正在加载"
                : `每页 ${filters.pageSize} 条 · 本页 ${listQuery.data.items.length} 条`}
            </Typography>
            <Stack direction="row" spacing={1}>
              <Button onClick={goToFirstPage} disabled={filters.cursor === undefined}>
                首页
              </Button>
              <Button
                onClick={goToNextPage}
                disabled={listQuery.data?.page.nextCursor === null || listQuery.data === undefined}
              >
                下一页
              </Button>
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      {dialogState?.kind === "create" ? (
        <UserEditorDialog mode="create" actor={user} onClose={closeDialog} />
      ) : null}
      {dialogState?.kind === "edit" && dialogState.userId !== undefined ? (
        <UserEditorDialog
          mode="edit"
          userId={dialogState.userId}
          actor={user}
          onClose={closeDialog}
        />
      ) : null}
      {dialogState?.kind === "delete" && dialogState.userId !== undefined ? (
        <UserActionDialog kind="delete" userId={dialogState.userId} onClose={closeDialog} />
      ) : null}
      {dialogState?.kind === "reset-password" && dialogState.userId !== undefined ? (
        <UserActionDialog kind="reset-password" userId={dialogState.userId} onClose={closeDialog} />
      ) : null}
    </Stack>
  );
}

/** Reserve management-page geometry while the authenticated identity query reaches React. */
function UserManagementLoading() {
  return (
    <Stack spacing={3} aria-label="正在恢复用户管理会话">
      <Skeleton variant="text" width={160} height={48} />
      <Skeleton variant="rounded" height={112} />
      <Skeleton variant="rounded" height={420} />
    </Stack>
  );
}
