import { Alert, Box, Card, CardContent, Skeleton, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { manageableUserStatisticsQueryOptions } from "../../../api/users";

/** 渲染用户状态分布；复用同一 Query key，不产生重复网络请求。 */
export function UserStatusDistribution() {
  const query = useQuery(manageableUserStatisticsQueryOptions());
  const statistics = query.data;

  return (
    <Card component="section" aria-labelledby="user-status-title">
      <CardContent sx={{ minHeight: 306 }}>
        <Typography id="user-status-title" component="h2" variant="h5">
          用户状态
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
          仅统计当前角色可管理的 USER 与 ADMIN。
        </Typography>
        {query.isPending ? (
          <Stack spacing={2}>
            <Skeleton variant="rounded" height={24} />
            <Skeleton variant="rounded" height={24} />
            <Skeleton variant="rounded" height={24} />
          </Stack>
        ) : null}
        {query.isError && statistics === undefined ? (
          <Alert severity="warning">用户状态统计暂不可用。</Alert>
        ) : null}
        {statistics === undefined ? null : (
          <Stack spacing={2}>
            <DistributionRow
              label="普通用户"
              value={roleTotal(statistics.byRole, "USER")}
              total={statistics.total}
            />
            <DistributionRow
              label="管理员"
              value={roleTotal(statistics.byRole, "ADMIN")}
              total={statistics.total}
            />
            <DistributionRow label="已禁用" value={statistics.disabled} total={statistics.total} />
            <DistributionRow
              label="近30天登录"
              value={statistics.loggedInLast30Days}
              total={statistics.total}
            />
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}

/** 从角色统计中读取一个角色总数。 */
function roleTotal(
  byRole: Array<{ role: "USER" | "ADMIN"; total: number }>,
  role: "USER" | "ADMIN",
): number {
  return byRole.find((item) => item.role === role)?.total ?? 0;
}

/** 描述用户状态分布条。 */
interface DistributionRowProps {
  label: string;
  value: number;
  total: number;
}

/** 用文字、数值和长度共同表达用户状态比例。 */
function DistributionRow({ label, value, total }: DistributionRowProps) {
  const width = total === 0 ? 0 : Math.round((value / total) * 100);

  return (
    <Stack direction="row" spacing={1.5} alignItems="center">
      <Typography variant="subtitle2" sx={{ width: 74 }}>
        {label}
      </Typography>
      <Box sx={{ flex: 1, height: 8, borderRadius: 1, bgcolor: "grey.200", overflow: "hidden" }}>
        <Box
          sx={{
            width: `${width}%`,
            height: "100%",
            borderRadius: 1,
            bgcolor: label === "已禁用" ? "grey.400" : "primary.main",
          }}
        />
      </Box>
      <Typography variant="body2" fontWeight={700} sx={{ width: 34, textAlign: "right" }}>
        {value}
      </Typography>
    </Stack>
  );
}
