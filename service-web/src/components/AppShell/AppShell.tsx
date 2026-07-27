import { Box } from "@mui/material";
import { Navigate, Outlet, useLocation } from "react-router-dom";

import { authSession } from "../../api/auth-session";
import { appLayout } from "../../styles/design-tokens";
import { useAuth } from "../AuthProvider";
import { RouteLoadingView } from "../RouteLoadingView";
import { AppHeader } from "./components/AppHeader";
import { AppSidebar } from "./components/AppSidebar";
import { useSessionInvalidation } from "./hooks/useSessionInvalidation";

/** 渲染固定桌面导航、账号控制与嵌套受保护路由。 */
export function AppShell() {
  const { status, user, hasPermission } = useAuth();
  const location = useLocation();
  const sessionInvalidated = useSessionInvalidation();

  if (
    status === "anonymous" ||
    authSession.getSnapshot().status === "anonymous" ||
    sessionInvalidated
  ) {
    const returnTo = `${location.pathname}${location.search}${location.hash}`;

    return (
      <Navigate
        replace
        to={`/login?returnTo=${encodeURIComponent(returnTo)}&reason=session-expired`}
      />
    );
  }

  if (user === undefined) {
    return <RouteLoadingView label="正在恢复会话" />;
  }

  return (
    <Box
      sx={{
        minHeight: "100vh",
        minWidth: appLayout.desktopMinWidth,
        display: "flex",
        bgcolor: "background.default",
      }}
    >
      <AppSidebar pathname={location.pathname} hasPermission={hasPermission} />
      <Box
        sx={{
          minWidth: 0,
          flex: 1,
          display: "flex",
          flexDirection: "column",
          bgcolor: "grey.200",
        }}
      >
        <AppHeader user={user} />
        <Box
          component="main"
          id="main-content"
          sx={{
            width: "100%",
            maxWidth: appLayout.analyticsMaxWidth,
            minHeight: `calc(100vh - ${appLayout.appBarDesktopHeight}px)`,
            mx: "auto",
            px: 3,
            py: 3,
            "@media (min-width: 1440px)": { px: 4, py: 4 },
            "@media (min-width: 1920px)": { px: 5 },
          }}
        >
          <Outlet />
        </Box>
      </Box>
    </Box>
  );
}
