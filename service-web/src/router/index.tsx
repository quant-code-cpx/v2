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
          /** 加载不含市场或图表 fixture 的受保护首页占位。 */
          lazy: async () => {
            const { HomePlaceholderView } =
              await import("../views/HomePlaceholderView/HomePlaceholderView");

            return { Component: HomePlaceholderView };
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
