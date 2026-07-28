import { RefreshOutlined as RefreshOutlinedIcon } from "@mui/icons-material";
import { Box, Breadcrumbs, Button, Stack, Typography } from "@mui/material";

import { AccountStatusCard } from "./components/AccountStatusCard";
import { RecentAuditEventsCard, RecentAuditMetricCard } from "./components/RecentAuditEventsCard";
import { SessionStatusCard } from "./components/SessionStatusCard";
import { UserStatisticsCard } from "./components/UserStatisticsCard";
import { UserStatusDistribution } from "./components/UserStatusDistribution";
import { WorkspaceQuickActions } from "./components/WorkspaceQuickActions";
import { useWorkspace } from "./hooks/useWorkspace";

/** 组合按权限并行加载且可独立失败的平台工作台。 */
export function WorkspaceView() {
  const model = useWorkspace();

  return (
    <Stack spacing={2.5}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Breadcrumbs aria-label="当前位置" separator="/" sx={{ mb: 0.75 }}>
            <Typography variant="body2" color="text.secondary">
              工作区
            </Typography>
            <Typography variant="body2" color="text.primary">
              平台工作台
            </Typography>
          </Breadcrumbs>
          <Typography component="h1" variant="h3">
            平台工作台
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            账户安全、用户运营与关键操作概览。
          </Typography>
        </Box>
        <Button
          variant="outlined"
          startIcon={<RefreshOutlinedIcon />}
          onClick={() => void model.refresh()}
        >
          刷新数据
        </Button>
      </Stack>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: `repeat(${1 + Number(model.canReadSessions) + Number(model.canReadUsers) + Number(model.canReadAudit)}, minmax(0, 1fr))`,
          gap: 2.5,
        }}
      >
        <AccountStatusCard user={model.user} />
        {model.canReadSessions ? <SessionStatusCard /> : null}
        {model.canReadUsers ? <UserStatisticsCard /> : null}
        {model.canReadAudit ? <RecentAuditMetricCard /> : null}
      </Box>
      {model.canReadUsers || model.canReadAudit ? (
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns:
              model.canReadUsers && model.canReadAudit
                ? "minmax(0, 3fr) minmax(360px, 2fr)"
                : "minmax(0, 1fr)",
            gap: 2.5,
          }}
        >
          {model.canReadUsers ? <UserStatusDistribution /> : null}
          {model.canReadAudit ? <RecentAuditEventsCard /> : null}
        </Box>
      ) : null}
      <WorkspaceQuickActions canReadUsers={model.canReadUsers} canReadAudit={model.canReadAudit} />
    </Stack>
  );
}
