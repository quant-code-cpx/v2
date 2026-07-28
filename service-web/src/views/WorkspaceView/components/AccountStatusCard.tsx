import { ShieldOutlined as ShieldOutlinedIcon } from "@mui/icons-material";
import { Box, Card, CardContent, Chip, Skeleton, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";

import { brandColors } from "../../../styles/design-tokens";
import type { CurrentUser } from "../../../types/access";

/** 描述工作台账户状态卡所需身份。 */
interface AccountStatusCardProps {
  user: CurrentUser | undefined;
}

/** 格式化最近登录时间。 */
const loginTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "short",
  timeStyle: "short",
});

/** 渲染当前权威身份的账户状态，不额外发起远程请求。 */
export function AccountStatusCard({ user }: AccountStatusCardProps) {
  return (
    <Card>
      <CardContent sx={{ minHeight: 152 }}>
        <Stack direction="row" justifyContent="space-between">
          <Typography variant="subtitle2" color="text.secondary">
            账号状态
          </Typography>
          <MetricIcon>
            <ShieldOutlinedIcon fontSize="small" />
          </MetricIcon>
        </Stack>
        {user === undefined ? (
          <Skeleton variant="text" width="60%" height={48} />
        ) : (
          <>
            <Typography variant="h3" sx={{ mt: 1 }}>
              {user.status === "ACTIVE" ? "安全" : "需处理"}
            </Typography>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 0.5 }}>
              <Chip color={user.status === "ACTIVE" ? "success" : "warning"} label="● 正常" />
              <Typography variant="caption" color="text.secondary">
                最近登录{" "}
                {user.lastLoginAt === null
                  ? "暂无"
                  : loginTimeFormatter.format(new Date(user.lastLoginAt))}
              </Typography>
            </Stack>
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** 描述工作台指标图标容器。 */
interface MetricIconProps {
  children: ReactNode;
}

/** 渲染使用 canonical 主色的指标图标容器。 */
export function MetricIcon({ children }: MetricIconProps) {
  return (
    <Box
      sx={{
        width: 40,
        height: 40,
        display: "grid",
        placeItems: "center",
        borderRadius: 1,
        bgcolor: brandColors.primaryLighter,
        color: "primary.main",
      }}
    >
      {children}
    </Box>
  );
}
