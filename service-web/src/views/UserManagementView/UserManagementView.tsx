import { AddOutlined as AddOutlinedIcon } from "@mui/icons-material";
import { Box, Breadcrumbs, Button, Skeleton, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";

import type { CurrentUser } from "../../types/access";
import type { UserDialogState } from "../../utils/user-list-url";
import { UserActionDialog } from "./components/UserActionDialog";
import { UserEditorDialog } from "./components/UserEditorDialog";
import { UserFilters } from "./components/UserFilters";
import { UserTable } from "./components/UserTable";
import { useUserManagement } from "./hooks/useUserManagement";
import { userListUpdatedTimeFormatter } from "./utils/user-date-formatters";

/** 组合受保护、URL 驱动的用户管理页面。 */
export function UserManagementView() {
  const model = useUserManagement();
  const { user } = model;

  if (user === undefined) {
    return (
      <Stack spacing={3} aria-label="正在恢复用户管理会话">
        <Skeleton variant="text" width={160} height={48} />
        <Skeleton variant="rounded" height={112} />
        <Skeleton variant="rounded" height={420} />
      </Stack>
    );
  }

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
        {model.canCreate ? (
          <Button
            variant="contained"
            size="large"
            startIcon={<AddOutlinedIcon />}
            onClick={model.handleCreate}
          >
            新建用户
          </Button>
        ) : null}
      </Stack>
      <UserFilters
        filters={model.filters}
        canManageAdmins={model.canManageAdmins}
        canSeeDeleted={model.canSeeDeleted}
        isFetching={model.listQuery.isFetching}
        onSearchChange={model.handleSearchChange}
        onRoleChange={model.handleRoleChange}
        onStatusChange={model.handleStatusChange}
        onRefresh={model.refreshList}
        onReset={model.resetFilters}
      />
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ px: 0.5 }}>
        <Stack direction="row" spacing={1} alignItems="baseline">
          <Typography fontWeight={700}>全部用户</Typography>
          <Typography variant="body2" color="text.secondary">
            {model.listQuery.data === undefined
              ? "正在加载"
              : `本页 ${model.listQuery.data.items.length} 个账号`}
          </Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary">
          {model.listQuery.dataUpdatedAt === 0
            ? "尚未更新"
            : `更新于 ${userListUpdatedTimeFormatter.format(
                new Date(model.listQuery.dataUpdatedAt),
              )}`}
        </Typography>
      </Stack>
      <UserTable
        actor={user}
        filters={model.filters}
        data={model.listQuery.data}
        isPending={model.listQuery.isPending}
        isError={model.listQuery.isError}
        canUpdate={model.canUpdate}
        canResetPassword={model.canResetPassword}
        canDelete={model.canDelete}
        onSort={model.handleTableSort}
        onEdit={model.handleEdit}
        onResetPassword={model.handleResetPassword}
        onDelete={model.handleDelete}
        onResetFilters={model.resetFilters}
        onRetry={model.refreshList}
        onFirstPage={model.goToFirstPage}
        onNextPage={model.goToNextPage}
      />
      {renderUserDialog(model.dialogState, user, model.closeDialog)}
    </Stack>
  );
}

/** 根据 URL 状态只挂载一个用户管理 Dialog。 */
function renderUserDialog(
  dialogState: UserDialogState | undefined,
  actor: CurrentUser,
  onClose: () => void,
): ReactNode {
  if (dialogState?.kind === "create") {
    return <UserEditorDialog mode="create" actor={actor} onClose={onClose} />;
  }
  if (dialogState?.kind === "edit" && dialogState.userId !== undefined) {
    return (
      <UserEditorDialog mode="edit" userId={dialogState.userId} actor={actor} onClose={onClose} />
    );
  }
  if (dialogState?.kind === "delete" && dialogState.userId !== undefined) {
    return <UserActionDialog kind="delete" userId={dialogState.userId} onClose={onClose} />;
  }
  if (dialogState?.kind === "reset-password" && dialogState.userId !== undefined) {
    return <UserActionDialog kind="reset-password" userId={dialogState.userId} onClose={onClose} />;
  }

  return null;
}
