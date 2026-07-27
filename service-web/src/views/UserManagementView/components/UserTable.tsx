import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";

import type { User, UserListFilters, UserPage } from "../../../types/access";
import { SortableTableHeader } from "./SortableTableHeader";
import { UserTableRow } from "./UserTableRow";
import { UserTableEmpty, UserTableSkeleton } from "./UserTableStates";

/** 描述用户表格远程状态、权限与页面动作。 */
interface UserTableProps {
  actor: User;
  filters: UserListFilters;
  data: UserPage | undefined;
  isPending: boolean;
  isError: boolean;
  canUpdate: boolean;
  canResetPassword: boolean;
  canDelete: boolean;
  onSort: (sort: NonNullable<UserListFilters["sort"]>) => void;
  onEdit: (userId: string) => void;
  onResetPassword: (userId: string) => void;
  onDelete: (userId: string) => void;
  onResetFilters: () => void;
  onRetry: () => void;
  onFirstPage: () => void;
  onNextPage: () => void;
}

/** 渲染用户表格的加载、空、错误、结果与 cursor 分页状态。 */
export function UserTable({
  actor,
  filters,
  data,
  isPending,
  isError,
  canUpdate,
  canResetPassword,
  canDelete,
  onSort,
  onEdit,
  onResetPassword,
  onDelete,
  onResetFilters,
  onRetry,
  onFirstPage,
  onNextPage,
}: UserTableProps) {
  return (
    <Card>
      <CardContent sx={{ p: 0 }}>
        {isError ? (
          <Box sx={{ p: 3 }}>
            <Alert
              severity="error"
              action={
                <Button color="inherit" size="small" onClick={onRetry}>
                  重试
                </Button>
              }
            >
              用户列表暂时不可用，请稍后重试。
            </Alert>
          </Box>
        ) : null}
        {isError && data === undefined ? null : (
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
                    onSort={onSort}
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
                    onSort={onSort}
                    width="14%"
                  />
                  <SortableTableHeader
                    label="创建时间"
                    sort="createdAt"
                    activeSort={filters.sort}
                    order={filters.order}
                    onSort={onSort}
                    width="14%"
                  />
                  <TableCell width="10%" align="right">
                    操作
                  </TableCell>
                </TableRow>
              </TableHead>
              {isPending || data === undefined ? <UserTableSkeleton /> : null}
              {data !== undefined && data.items.length === 0 ? (
                <UserTableEmpty onReset={onResetFilters} />
              ) : null}
              {data !== undefined && data.items.length > 0 ? (
                <TableBody>
                  {/* 每个结果使用独立行组件，避免表格职责回流到页面。 */}
                  {data.items.map((managedUser) => (
                    <UserTableRow
                      key={managedUser.id}
                      user={managedUser}
                      isSelf={managedUser.id === actor.id}
                      canUpdate={canUpdate}
                      canResetPassword={canResetPassword}
                      canDelete={canDelete}
                      onEdit={onEdit}
                      onResetPassword={onResetPassword}
                      onDelete={onDelete}
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
            {data === undefined
              ? "正在加载"
              : `每页 ${filters.pageSize} 条 · 本页 ${data.items.length} 条`}
          </Typography>
          <Stack direction="row" spacing={1}>
            <Button onClick={onFirstPage} disabled={filters.cursor === undefined}>
              首页
            </Button>
            <Button
              onClick={onNextPage}
              disabled={data?.page.nextCursor === null || data === undefined}
            >
              下一页
            </Button>
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}
