import {
  AccessTimeOutlined as AccessTimeOutlinedIcon,
  KeyOutlined as KeyOutlinedIcon,
  MonitorOutlined as MonitorOutlinedIcon,
} from "@mui/icons-material";
import { Box, Button, Card, CardContent, Chip, Divider, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import type { ReactNode } from "react";

import type { CurrentUser } from "../../../types/access";
import type { useAccount } from "../hooks/useAccount";

/** 描述账户安全摘要所需的身份与页面模型。 */
interface AccountSecurityCardProps {
  user: CurrentUser | undefined;
  model: ReturnType<typeof useAccount>;
}

/** 格式化最近登录时间，避免推断设备或位置。 */
const loginTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "short",
});

/** 渲染密码、最近登录和 Session family 数量摘要。 */
export function AccountSecurityCard({ user, model }: AccountSecurityCardProps) {
  const sessionTotal = model.sessionQuery.data?.total;

  return (
    <Card component="section" aria-labelledby="account-security-title">
      <CardContent>
        <Typography id="account-security-title" component="h2" variant="h5">
          账户安全
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
          安全动作会生成审计记录。
        </Typography>
        <SecurityRow
          icon={<KeyOutlinedIcon fontSize="small" />}
          title="登录密码"
          description="修改后全部会话失效，需要重新登录"
          action={
            <Button
              variant="outlined"
              onClick={() => model.openDialog({ kind: "change-password" })}
              disabled={!user?.permissions.includes("password:change")}
            >
              修改
            </Button>
          }
        />
        <Divider />
        <SecurityRow
          icon={<AccessTimeOutlinedIcon fontSize="small" />}
          title="最近登录"
          description={
            user?.lastLoginAt === null || user?.lastLoginAt === undefined
              ? "暂无成功登录时间"
              : loginTimeFormatter.format(new Date(user.lastLoginAt))
          }
          action={<Chip color="success" label="正常" />}
        />
        <Divider />
        <SecurityRow
          icon={<MonitorOutlinedIcon fontSize="small" />}
          title="活动会话"
          description={
            model.canReadSessions
              ? sessionTotal === undefined
                ? "正在读取活动 family"
                : `当前账号共有 ${sessionTotal} 个活动 family`
              : "当前身份缺少 Session 查看权限"
          }
          action={
            <Chip
              color={model.canReadSessions ? "info" : "default"}
              label={sessionTotal === undefined ? "—" : String(sessionTotal)}
            />
          }
        />
      </CardContent>
    </Card>
  );
}

/** 描述一行不包含设备数据的安全摘要。 */
interface SecurityRowProps {
  icon: ReactNode;
  title: string;
  description: string;
  action: ReactNode;
}

/** 渲染统一几何的账户安全摘要行。 */
function SecurityRow({ icon, title, description, action }: SecurityRowProps) {
  return (
    <Stack direction="row" alignItems="center" spacing={1.5} sx={{ py: 2 }}>
      <Box
        sx={(theme) => ({
          width: 40,
          height: 40,
          display: "grid",
          placeItems: "center",
          borderRadius: 1,
          color: "primary.main",
          bgcolor: alpha(theme.palette.primary.main, 0.1),
        })}
      >
        {icon}
      </Box>
      <Box sx={{ minWidth: 0, flex: 1 }}>
        <Typography variant="subtitle2">{title}</Typography>
        <Typography variant="body2" color="text.secondary">
          {description}
        </Typography>
      </Box>
      {action}
    </Stack>
  );
}
