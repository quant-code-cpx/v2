import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "../components/AppShell/AppShell";
import { RouteLoadingView } from "../components/RouteLoadingView";
import { ForbiddenView } from "../views/ForbiddenView/ForbiddenView";
import { RouteErrorView } from "../views/RouteErrorView/RouteErrorView";
import { redirectAuthenticatedLogin, requirePermission, requireSession } from "./guards";

/** 创建匿名登录与默认拒绝的受保护路由树。 */
export function createAppRouter() {
  return createBrowserRouter([
    {
      path: "/login",
      loader: redirectAuthenticatedLogin,
      errorElement: <RouteErrorView />,
      HydrateFallback: RouteLoadingView,
      /** 加载不含图表依赖的隔离匿名登录路由。 */
      lazy: async () => {
        const { LoginView } = await import("../views/LoginView/LoginView");

        return { Component: LoginView };
      },
    },
    {
      path: "/",
      Component: AppShell,
      loader: requireSession,
      errorElement: <RouteErrorView />,
      HydrateFallback: RouteLoadingView,
      children: [
        {
          index: true,
          /** 按权限加载不含图表依赖的平台工作台。 */
          lazy: async () => {
            const { WorkspaceView } = await import("../views/WorkspaceView/WorkspaceView");

            return { Component: WorkspaceView };
          },
        },
        {
          path: "account",
          loader: requireSession,
          /** 会话验证后加载本人资料与安全管理页面。 */
          lazy: async () => {
            const { AccountView } = await import("../views/AccountView/AccountView");

            return { Component: AccountView };
          },
        },
        {
          path: "users",
          loader: requirePermission("users:read"),
          errorElement: <ForbiddenView />,
          /** 权限验证完成后才加载管理工作区。 */
          lazy: async () => {
            const { UserManagementView } =
              await import("../views/UserManagementView/UserManagementView");

            return { Component: UserManagementView };
          },
        },
        {
          path: "security/audit",
          loader: requirePermission("audit:read"),
          errorElement: <ForbiddenView />,
          /** 权限验证完成后才加载审计页面和查询代码。 */
          lazy: async () => {
            const { AuditEventsView } = await import("../views/AuditEventsView/AuditEventsView");

            return { Component: AuditEventsView };
          },
        },
        {
          path: "instruments/:symbol",
          loader: requireSession,
          /** 请求标的路由时才加载分析 Bundle。 */
          lazy: async () => {
            const { InstrumentAnalysisView } =
              await import("../views/InstrumentAnalysisView/InstrumentAnalysisView");

            return { Component: InstrumentAnalysisView };
          },
        },
        {
          path: "*",
          Component: RouteErrorView,
        },
      ],
    },
  ]);
}
