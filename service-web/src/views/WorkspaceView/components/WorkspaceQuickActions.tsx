import {
  ArrowForwardOutlined as ArrowForwardOutlinedIcon,
  AssignmentOutlined as AssignmentOutlinedIcon,
  ManageAccountsOutlined as ManageAccountsOutlinedIcon,
  PersonOutlineOutlined as PersonOutlineOutlinedIcon,
} from "@mui/icons-material";
import { Button, Card, CardContent, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";

/** 描述按权限显示的工作台快捷任务。 */
interface WorkspaceQuickActionsProps {
  canReadUsers: boolean;
  canReadAudit: boolean;
}

/** 渲染个人资料、用户管理和安全审计的可执行入口。 */
export function WorkspaceQuickActions({ canReadUsers, canReadAudit }: WorkspaceQuickActionsProps) {
  return (
    <Card component="section" aria-labelledby="quick-actions-title">
      <CardContent>
        <Typography id="quick-actions-title" component="h2" variant="h5">
          快捷任务
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
          按服务端权限显示可执行入口。
        </Typography>
        <Stack direction="row" spacing={1.5}>
          <QuickAction
            icon={<PersonOutlineOutlinedIcon />}
            title="维护个人资料"
            description="显示名称与账户安全"
            to="/account"
          />
          {canReadUsers ? (
            <QuickAction
              icon={<ManageAccountsOutlinedIcon />}
              title="管理平台用户"
              description="查询、创建与维护账号"
              to="/users"
            />
          ) : null}
          {canReadAudit ? (
            <QuickAction
              icon={<AssignmentOutlinedIcon />}
              title="检查安全审计"
              description="查看关键操作和请求标识"
              to="/security/audit"
            />
          ) : null}
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 描述一个明确路由目标的快捷任务。 */
interface QuickActionProps {
  icon: ReactNode;
  title: string;
  description: string;
  to: string;
}

/** 渲染可键盘访问的横向快捷任务按钮。 */
function QuickAction({ icon, title, description, to }: QuickActionProps) {
  return (
    <Button
      component={RouterLink}
      to={to}
      variant="outlined"
      startIcon={icon}
      endIcon={<ArrowForwardOutlinedIcon />}
      sx={{ flex: 1, minHeight: 64, justifyContent: "flex-start", textAlign: "left" }}
    >
      <span>
        <Typography component="span" variant="subtitle2" display="block">
          {title}
        </Typography>
        <Typography component="span" variant="caption" color="text.secondary">
          {description}
        </Typography>
      </span>
    </Button>
  );
}
