import {
  DeleteOutlineOutlined as DeleteOutlineOutlinedIcon,
  EditOutlined as EditOutlinedIcon,
  KeyOutlined as KeyOutlinedIcon,
} from "@mui/icons-material";
import {
  Avatar,
  Box,
  Chip,
  IconButton,
  Stack,
  TableCell,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import { useCallback } from "react";

import type { User } from "../../../types/access";
import { userRoleLabel, userStatusLabel } from "../../../utils/user-presentation";
import { userDateTimeFormatter } from "../utils/user-date-formatters";

/** 描述用户表格行的权限与目标动作。 */
interface UserTableRowProps {
  user: User;
  isSelf: boolean;
  canUpdate: boolean;
  canResetPassword: boolean;
  canDelete: boolean;
  onEdit: (userId: string) => void;
  onResetPassword: (userId: string) => void;
  onDelete: (userId: string) => void;
}

/** 渲染角色与状态感知的用户行，并按权限过滤管理动作。 */
export function UserTableRow({
  user,
  isSelf,
  canUpdate,
  canResetPassword,
  canDelete,
  onEdit,
  onResetPassword,
  onDelete,
}: UserTableRowProps) {
  const isDeleted = user.status === "DELETED";
  const isReadOnly = isDeleted || isSelf;

  /** 打开当前目标的编辑 Dialog，不把账号数据写入 URL。 */
  const handleEdit = useCallback(() => {
    onEdit(user.id);
  }, [onEdit, user.id]);

  /** 打开当前目标的密码重置 Dialog。 */
  const handleResetPassword = useCallback(() => {
    onResetPassword(user.id);
  }, [onResetPassword, user.id]);

  /** 打开当前目标的显式删除确认。 */
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
          : userDateTimeFormatter.format(new Date(user.lastLoginAt))}
      </TableCell>
      <TableCell sx={{ px: 1.5, whiteSpace: "nowrap" }}>
        {userDateTimeFormatter.format(new Date(user.updatedAt))}
      </TableCell>
      <TableCell sx={{ px: 1.5, whiteSpace: "nowrap" }}>
        {userDateTimeFormatter.format(new Date(user.createdAt))}
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
