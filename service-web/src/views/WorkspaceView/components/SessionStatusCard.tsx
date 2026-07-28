import { MonitorOutlined as MonitorOutlinedIcon } from "@mui/icons-material";
import { Alert, Card, CardContent, Skeleton, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { sessionFamiliesQueryOptions } from "../../../api/account-security";
import { MetricIcon } from "./AccountStatusCard";

/** 渲染活动 Session family 指标，并把失败限制在本卡片。 */
export function SessionStatusCard() {
  const query = useQuery(sessionFamiliesQueryOptions({ pageSize: 50 }));
  const nearestExpiry = query.data?.items
    .map((family) => family.absoluteExpiresAt)
    .toSorted()
    .at(0);

  return (
    <Card>
      <CardContent sx={{ minHeight: 152 }}>
        <Stack direction="row" justifyContent="space-between">
          <Typography variant="subtitle2" color="text.secondary">
            活动会话
          </Typography>
          <MetricIcon>
            <MonitorOutlinedIcon fontSize="small" />
          </MetricIcon>
        </Stack>
        {query.isPending ? <Skeleton variant="text" width="60%" height={48} /> : null}
        {query.isError && query.data === undefined ? (
          <Alert severity="warning" sx={{ mt: 1 }}>
            会话摘要暂不可用
          </Alert>
        ) : null}
        {query.data === undefined ? null : (
          <>
            <Typography variant="h3" sx={{ mt: 1 }}>
              {query.data.total}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              当前会话 · 最近过期{" "}
              {nearestExpiry === undefined
                ? "暂无"
                : new Intl.DateTimeFormat("zh-CN", {
                    month: "2-digit",
                    day: "2-digit",
                  }).format(new Date(nearestExpiry))}
            </Typography>
          </>
        )}
      </CardContent>
    </Card>
  );
}
