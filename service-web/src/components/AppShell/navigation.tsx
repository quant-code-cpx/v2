import {
  AssignmentOutlined as AssignmentOutlinedIcon,
  DataObjectOutlined as DataObjectOutlinedIcon,
  DashboardOutlined as DashboardOutlinedIcon,
  InsertChartOutlined as InsertChartOutlinedIcon,
  ManageAccountsOutlined as ManageAccountsOutlinedIcon,
  PersonOutlineOutlined as PersonOutlineOutlinedIcon,
  PublicOutlined as PublicOutlinedIcon,
  SegmentOutlined as SegmentOutlinedIcon,
  SwapHorizOutlined as SwapHorizOutlinedIcon,
  TableRowsOutlined as TableRowsOutlinedIcon,
} from "@mui/icons-material";
import type { ReactNode } from "react";

import type { Permission, UserRole } from "../../types/access";

/** 描述一个稳定侧栏目标及其所需服务端权限。 */
export interface NavigationItem {
  label: string;
  to: string;
  requiredPermission?: Permission;
  allowedRoles?: UserRole[];
  activePrefixes?: readonly string[];
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
      label: "市场",
      items: [
        {
          label: "市场概览",
          to: "/market",
          icon: <PublicOutlinedIcon fontSize="small" />,
        },
        {
          label: "股票中心",
          to: "/market/equities",
          activePrefixes: ["/market/equities"],
          icon: <TableRowsOutlinedIcon fontSize="small" />,
        },
        {
          label: "行业与板块",
          to: "/market/sectors",
          activePrefixes: ["/market/sectors", "/market/industries/sw"],
          icon: <SegmentOutlinedIcon fontSize="small" />,
        },
        {
          label: "基金与 ETF",
          to: "/market/funds",
          activePrefixes: ["/market/etfs"],
          icon: <InsertChartOutlinedIcon fontSize="small" />,
        },
        {
          label: "互联互通",
          to: "/market/stock-connect",
          activePrefixes: ["/market/stock-connect"],
          icon: <SwapHorizOutlinedIcon fontSize="small" />,
        },
      ],
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
      label: "数据管理",
      items: [
        {
          label: "数据运维",
          to: "/data-operations",
          allowedRoles: ["ADMIN", "SUPER_ADMIN"],
          icon: <DataObjectOutlinedIcon fontSize="small" />,
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
