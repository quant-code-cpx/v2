import { GroupOutlined as GroupOutlinedIcon } from "@mui/icons-material";
import { Alert, Card, CardContent, Skeleton, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { manageableUserStatisticsQueryOptions } from "../../../api/users";
import { MetricIcon } from "./AccountStatusCard";

/** 渲染当前管理员角色范围内的用户指标，并独立处理失败。 */
export function UserStatisticsCard() {
  const query = useQuery(manageableUserStatisticsQueryOptions());

  return (
    <Card>
      <CardContent sx={{ minHeight: 152 }}>
        <Stack direction="row" justifyContent="space-between">
          <Typography variant="subtitle2" color="text.secondary">
            可管理用户
          </Typography>
          <MetricIcon>
            <GroupOutlinedIcon fontSize="small" />
          </MetricIcon>
        </Stack>
        {query.isPending ? <Skeleton variant="text" width="60%" height={48} /> : null}
        {query.isError && query.data === undefined ? (
          <Alert severity="warning" sx={{ mt: 1 }}>
            用户统计暂不可用
          </Alert>
        ) : null}
        {query.data === undefined ? null : (
          <>
            <Typography variant="h3" sx={{ mt: 1 }}>
              {query.data.total}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              启用 {query.data.active} · 禁用 {query.data.disabled} · 已删除 {query.data.deleted}
            </Typography>
          </>
        )}
      </CardContent>
    </Card>
  );
}
