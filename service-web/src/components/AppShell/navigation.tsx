import {
  AssignmentOutlined as AssignmentOutlinedIcon,
  DashboardOutlined as DashboardOutlinedIcon,
  ManageAccountsOutlined as ManageAccountsOutlinedIcon,
  PersonOutlineOutlined as PersonOutlineOutlinedIcon,
} from "@mui/icons-material";
import type { ReactNode } from "react";

import type { Permission } from "../../types/access";

/** 描述一个稳定侧栏目标及其所需服务端权限。 */
export interface NavigationItem {
  label: string;
  to: string;
  requiredPermission?: Permission;
  icon: ReactNode;
}

/** 描述按业务区域分组的导航项。 */
export interface NavigationGroup {
  label: string;
  items: NavigationItem[];
}

/** 返回稳定导航分组；未完成能力不进入侧栏。 */
export function createNavigationGroups(): NavigationGroup[] {
  return [
    {
      label: "工作区",
      items: [{ label: "平台工作台", to: "/", icon: <DashboardOutlinedIcon fontSize="small" /> }],
    },
    {
      label: "个人",
      items: [
        {
          label: "我的账户",
          to: "/account",
          icon: <PersonOutlineOutlinedIcon fontSize="small" />,
        },
      ],
    },
    {
      label: "系统管理",
      items: [
        {
          label: "用户管理",
          to: "/users",
          requiredPermission: "users:read",
          icon: <ManageAccountsOutlinedIcon fontSize="small" />,
        },
        {
          label: "安全审计",
          to: "/security/audit",
          requiredPermission: "audit:read",
          icon: <AssignmentOutlinedIcon fontSize="small" />,
        },
      ],
    },
  ];
}
