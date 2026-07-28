import { RefreshOutlined as RefreshOutlinedIcon } from "@mui/icons-material";
import { Box, Breadcrumbs, Button, Stack, Typography } from "@mui/material";

import { AuditEventDrawer } from "./components/AuditEventDrawer";
import { AuditEventTable } from "./components/AuditEventTable";
import { AuditFilters } from "./components/AuditFilters";
import { useAuditEvents } from "./hooks/useAuditEvents";

/** 格式化服务端实际应用的审计时间窗。 */
const windowFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "short",
  timeStyle: "short",
});

/** 组合 SUPER_ADMIN 审计筛选、脱敏表格和详情 Drawer。 */
export function AuditEventsView() {
  const model = useAuditEvents();
  const appliedWindow = model.listQuery.data?.appliedWindow;

  return (
    <Stack spacing={2.5}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Breadcrumbs aria-label="当前位置" separator="/" sx={{ mb: 0.75 }}>
            <Typography variant="body2" color="text.secondary">
              系统管理
            </Typography>
            <Typography variant="body2" color="text.primary">
              安全审计
            </Typography>
          </Breadcrumbs>
          <Typography component="h1" variant="h3">
            安全审计
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            查看脱敏的账户、认证和管理操作；仅超级管理员可访问。
          </Typography>
        </Box>
        <Button
          variant="outlined"
          startIcon={<RefreshOutlinedIcon />}
          disabled={model.listQuery.isFetching}
          onClick={() => void model.refresh()}
        >
          刷新
        </Button>
      </Stack>
      <AuditFilters
        state={model.urlState}
        isFetching={model.listQuery.isFetching}
        onApply={model.applyFilters}
        onReset={model.resetFilters}
      />
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ px: 0.5 }}>
        <Typography>
          <strong>审计事件</strong>{" "}
          <Typography component="span" variant="body2" color="text.secondary">
            本页 {model.listQuery.data?.items.length ?? 0} 条 · 默认排除例行 Token 轮换
          </Typography>
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {appliedWindow === undefined
            ? "时间窗正在加载"
            : `时间窗 ${windowFormatter.format(new Date(appliedWindow.occurredFrom))} — ${windowFormatter.format(
                new Date(appliedWindow.occurredTo),
              )}`}
        </Typography>
      </Stack>
      <AuditEventTable
        data={model.listQuery.data}
        isPending={model.listQuery.isPending}
        isError={model.listQuery.isError}
        isFetching={model.listQuery.isFetching}
        hasCursor={model.urlState.cursor !== undefined}
        onOpen={model.openEvent}
        onRetry={() => void model.refresh()}
        onFirstPage={model.goToFirstPage}
        onNextPage={model.goToNextPage}
      />
      <AuditEventDrawer model={model} />
    </Stack>
  );
}
