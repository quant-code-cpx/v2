import {
  RefreshOutlined as RefreshOutlinedIcon,
  RestartAltOutlined as RestartAltOutlinedIcon,
} from "@mui/icons-material";
import {
  Box,
  Button,
  Card,
  CardContent,
  IconButton,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
} from "@mui/material";
import type { ChangeEvent } from "react";

import type { UserListFilters } from "../../../types/access";

/** 描述 URL 驱动筛选带需要的状态与动作。 */
interface UserFiltersProps {
  filters: UserListFilters;
  canManageAdmins: boolean;
  canSeeDeleted: boolean;
  isFetching: boolean;
  onSearchChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onRoleChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onStatusChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onRefresh: () => void;
  onReset: () => void;
}

/** 渲染桌面筛选带，所有可分享状态由 URL 持有。 */
export function UserFilters({
  filters,
  canManageAdmins,
  canSeeDeleted,
  isFetching,
  onSearchChange,
  onRoleChange,
  onStatusChange,
  onRefresh,
  onReset,
}: UserFiltersProps) {
  return (
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
            onChange={onSearchChange}
          />
          <TextField select label="角色" value={filters.role ?? ""} onChange={onRoleChange}>
            <MenuItem value="">全部角色</MenuItem>
            <MenuItem value="USER">普通用户</MenuItem>
            {canManageAdmins ? <MenuItem value="ADMIN">管理员</MenuItem> : null}
          </TextField>
          <TextField select label="状态" value={filters.status ?? ""} onChange={onStatusChange}>
            <MenuItem value="">全部状态</MenuItem>
            <MenuItem value="ACTIVE">启用</MenuItem>
            <MenuItem value="DISABLED">已禁用</MenuItem>
            {canSeeDeleted ? <MenuItem value="DELETED">已删除</MenuItem> : null}
          </TextField>
          <Stack direction="row" spacing={0.5} alignItems="center" justifyContent="flex-end">
            <Tooltip title="刷新当前用户列表">
              <span>
                <IconButton aria-label="刷新用户列表" onClick={onRefresh} disabled={isFetching}>
                  <RefreshOutlinedIcon />
                </IconButton>
              </span>
            </Tooltip>
            <Button startIcon={<RestartAltOutlinedIcon />} onClick={onReset}>
              重置
            </Button>
          </Stack>
        </Box>
      </CardContent>
    </Card>
  );
}
