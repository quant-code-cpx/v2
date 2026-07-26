import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { ForbiddenView } from "../views/ForbiddenView";
import { RouteErrorView } from "../views/RouteErrorView";
import { redirectAuthenticatedLogin, requirePermission, requireSession } from "./guards";

/** Create an anonymous-login and default-deny protected route tree. */
export function createAppRouter() {
  return createBrowserRouter([
    {
      path: "/login",
      loader: redirectAuthenticatedLogin,
      errorElement: <RouteErrorView />,
      /** Load the isolated anonymous login route without chart dependencies. */
      lazy: async () => {
        const { LoginView } = await import("../views/LoginView");

        return { Component: LoginView };
      },
    },
    {
      path: "/",
      Component: AppShell,
      loader: requireSession,
      errorElement: <RouteErrorView />,
      children: [
        {
          index: true,
          /** Load protected homepage placeholder without market/chart fixtures. */
          lazy: async () => {
            const { HomePlaceholderView } = await import("../views/HomePlaceholderView");

            return { Component: HomePlaceholderView };
          },
        },
        {
          path: "users",
          loader: requirePermission("users:read"),
          errorElement: <ForbiddenView />,
          /** Load administration workspace only after permission verification completes. */
          lazy: async () => {
            const { UserManagementView } = await import("../views/UserManagementView");

            return { Component: UserManagementView };
          },
        },
        {
          path: "instruments/:symbol",
          loader: requireSession,
          /** Load analysis route bundle only when a symbol route is requested. */
          lazy: async () => {
            const { InstrumentAnalysisView } = await import("../views/InstrumentAnalysisView");

            return { Component: InstrumentAnalysisView };
          },
        },
        {
          path: "*",
          /** Load a protected not-found state for unknown application paths. */
          lazy: async () => {
            const { RouteErrorView } = await import("../views/RouteErrorView");

            return { Component: RouteErrorView };
          },
        },
      ],
    },
  ]);
}
