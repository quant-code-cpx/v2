import {
  Alert,
  Button,
  Card,
  Chip,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";

import type { AuditCategory, AuditEventPage } from "../../../types/account-security";
import { auditCategoryLabel, auditSeverityLabel } from "../utils/audit-event-presentation";

/** 格式化审计发生时间。 */
const auditTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "short",
  timeStyle: "medium",
});

/** 描述审计表格的查询状态与动作。 */
interface AuditEventTableProps {
  data: AuditEventPage | undefined;
  isPending: boolean;
  isError: boolean;
  isFetching: boolean;
  hasCursor: boolean;
  onOpen: (eventId: string) => void;
  onRetry: () => void;
  onFirstPage: () => void;
  onNextPage: () => void;
}

/** 渲染审计列表的 loading、empty、error/retry、stale 与游标状态。 */
export function AuditEventTable({
  data,
  isPending,
  isError,
  isFetching,
  hasCursor,
  onOpen,
  onRetry,
  onFirstPage,
  onNextPage,
}: AuditEventTableProps) {
  if (isPending) {
    return (
      <Card sx={{ p: 3 }} aria-label="正在加载审计事件">
        <Skeleton variant="rounded" height={56} />
        <Skeleton variant="rounded" height={64} sx={{ mt: 1 }} />
        <Skeleton variant="rounded" height={64} sx={{ mt: 1 }} />
        <Skeleton variant="rounded" height={64} sx={{ mt: 1 }} />
      </Card>
    );
  }

  if (isError && data === undefined) {
    return (
      <Card sx={{ p: 3 }}>
        <Alert
          severity="error"
          action={
            <Button color="inherit" size="small" onClick={onRetry}>
              重试
            </Button>
          }
        >
          审计事件加载失败，请稍后重新加载。
        </Alert>
      </Card>
    );
  }

  if (data?.items.length === 0) {
    return (
      <Card sx={{ p: 3 }}>
        <Alert severity="info">当前筛选范围没有审计事件，请调整条件后重试。</Alert>
      </Card>
    );
  }

  return (
    <Card component="section" aria-label="审计事件列表">
      {isError ? (
        <Alert severity="warning" sx={{ m: 2 }} action={<Button onClick={onRetry}>重试</Button>}>
          刷新失败，表格保留上次成功数据。
        </Alert>
      ) : null}
      <TableContainer tabIndex={0} aria-label="审计事件表格，可横向滚动">
        <Table aria-label="审计事件">
          <TableHead>
            <TableRow>
              <TableCell scope="col" sx={{ width: "18%" }}>
                时间
              </TableCell>
              <TableCell scope="col" sx={{ width: "17%" }}>
                分类
              </TableCell>
              <TableCell scope="col" sx={{ width: "31%" }}>
                事件
              </TableCell>
              <TableCell scope="col" sx={{ width: "18%" }}>
                Actor
              </TableCell>
              <TableCell scope="col" align="right">
                操作
              </TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {data?.items.map((event) => (
              <TableRow key={event.id} hover>
                <TableCell>{auditTimeFormatter.format(new Date(event.occurredAt))}</TableCell>
                <TableCell>
                  <CategoryChip category={event.category} />
                </TableCell>
                <TableCell>
                  <Typography variant="subtitle2">{event.summary}</Typography>
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ fontFamily: "monospace" }}
                    >
                      {event.action}
                    </Typography>
                    {event.severity === "INFO" ? null : (
                      <Typography variant="caption" color="warning.dark">
                        {auditSeverityLabel(event.severity)}
                      </Typography>
                    )}
                  </Stack>
                </TableCell>
                <TableCell>{event.actor?.displayName ?? "系统"}</TableCell>
                <TableCell align="right">
                  <Button onClick={() => onOpen(event.id)}>查看详情</Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      <Stack
        component="footer"
        direction="row"
        justifyContent="space-between"
        alignItems="center"
        sx={{ px: 2, py: 1.5 }}
      >
        <Typography variant="body2" color="text.secondary">
          每页 20 条 · 游标分页{isFetching ? " · 正在刷新" : ""}
        </Typography>
        <Stack direction="row" spacing={1}>
          <Button disabled={!hasCursor} onClick={onFirstPage}>
            首页
          </Button>
          <Button
            variant="outlined"
            disabled={data?.page.nextCursor === null || data?.page.nextCursor === undefined}
            onClick={onNextPage}
          >
            下一页
          </Button>
        </Stack>
      </Stack>
    </Card>
  );
}

/** 描述分类 Chip 的合同分类输入。 */
interface CategoryChipProps {
  category: AuditCategory;
}

/** 用非方向性色彩与文字同时表达审计分类。 */
function CategoryChip({ category }: CategoryChipProps) {
  const colorMap: Record<AuditCategory, "warning" | "primary" | "info" | "default"> = {
    AUTHENTICATION: "warning",
    ACCOUNT: "primary",
    USER_ADMINISTRATION: "info",
    SYSTEM: "default",
  };

  return (
    <Chip
      label={auditCategoryLabel(category)}
      color={colorMap[category]}
      sx={(theme) =>
        category === "SYSTEM"
          ? { bgcolor: alpha(theme.palette.text.secondary, 0.08), color: "text.secondary" }
          : {}
      }
    />
  );
}
