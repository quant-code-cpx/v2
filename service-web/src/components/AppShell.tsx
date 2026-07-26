import {
  DashboardOutlined as DashboardOutlinedIcon,
  HelpOutlineOutlined as HelpOutlineOutlinedIcon,
  KeyboardArrowDownOutlined as KeyboardArrowDownOutlinedIcon,
  LogoutOutlined as LogoutOutlinedIcon,
  ManageAccountsOutlined as ManageAccountsOutlinedIcon,
  NotificationsNoneOutlined as NotificationsNoneOutlinedIcon,
  SearchOutlined as SearchOutlinedIcon,
} from "@mui/icons-material";
import {
  Avatar,
  Box,
  Button,
  ButtonBase,
  CircularProgress,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Menu,
  MenuItem,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import { useCallback, useLayoutEffect, useState } from "react";
import { Link as RouterLink, Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import type { MouseEvent, ReactNode } from "react";

import { authSession, authSessionInvalidatedEvent } from "../api/auth-session";
import { appLayout, brandColors } from "../styles/design-tokens";
import { ApexBrand } from "./ApexBrand";
import { useAuth } from "./AuthProvider";
import { useFeedback } from "./FeedbackProvider";
import { userRoleLabel } from "../utils/user-presentation";

/** Describe one stable sidebar destination and its required server-calculated permission. */
interface NavigationItem {
  label: string;
  to: string;
  requiredPermission?: "users:read";
  icon: ReactNode;
}

/** Group stable destinations by business area while unfinished capabilities stay absent. */
const navigationGroups: { label: string; items: NavigationItem[] }[] = [
  {
    label: "工作区",
    items: [{ label: "首页", to: "/", icon: <DashboardOutlinedIcon fontSize="small" /> }],
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
    ],
  },
];

/** Return initials for a compact, non-sensitive signed-in identity marker. */
function userInitial(displayName: string): string {
  return displayName.trim().slice(0, 1).toUpperCase() || "A";
}

/** Render persistent desktop navigation, account controls, feedback area, and nested protected routes. */
export function AppShell() {
  const { status, user, hasPermission, logout } = useAuth();
  const { info } = useFeedback();
  const location = useLocation();
  const navigate = useNavigate();
  const [accountMenuAnchor, setAccountMenuAnchor] = useState<HTMLElement | null>(null);
  const [sessionInvalidated, setSessionInvalidated] = useState(false);

  /** Replace the protected shell immediately when an API 401 invalidates its credential. */
  useLayoutEffect(() => {
    /** Mark session expiry so the anonymous route can present recovery guidance after redirect. */
    const handleSessionInvalidation = () => {
      setSessionInvalidated(true);
    };

    window.addEventListener(authSessionInvalidatedEvent, handleSessionInvalidation);

    return () => {
      window.removeEventListener(authSessionInvalidatedEvent, handleSessionInvalidation);
    };
  }, []);

  /** Open the account actions menu from the authenticated identity control. */
  const handleAccountMenuOpen = useCallback((event: MouseEvent<HTMLElement>) => {
    setAccountMenuAnchor(event.currentTarget);
  }, []);

  /** Close the account actions menu without changing route or session state. */
  const handleAccountMenuClose = useCallback(() => {
    setAccountMenuAnchor(null);
  }, []);

  /** Explain the reserved search surface without inventing an unfrozen search endpoint. */
  const handleGlobalSearch = useCallback(() => {
    info("全局搜索功能即将开放。");
  }, [info]);

  /** Explain the reserved help surface without navigating to an unfinished page. */
  const handleHelp = useCallback(() => {
    info("帮助中心正在建设中。");
  }, [info]);

  /** Clear client session state and replace browser history with the anonymous login route. */
  const handleLogout = useCallback(async () => {
    handleAccountMenuClose();
    await logout();
    info("已退出登录。");
    void navigate("/login", { replace: true });
  }, [handleAccountMenuClose, info, logout, navigate]);

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
    return (
      <Box
        sx={{
          minHeight: "100vh",
          display: "grid",
          placeItems: "center",
          bgcolor: "background.default",
        }}
      >
        <CircularProgress aria-label="正在恢复会话" />
      </Box>
    );
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
      <Box
        component="aside"
        sx={{
          width: appLayout.sidebarWidth,
          flexShrink: 0,
          display: "flex",
          flexDirection: "column",
          borderRight: 1,
          borderColor: "divider",
          bgcolor: "background.paper",
        }}
      >
        <Box
          component={RouterLink}
          to="/"
          aria-label="Apex数据智能分析平台首页"
          sx={{
            minHeight: appLayout.appBarDesktopHeight,
            px: 3,
            display: "flex",
            alignItems: "center",
            color: "text.primary",
            textDecoration: "none",
            "&:hover .MuiTypography-root": { color: "primary.dark" },
          }}
        >
          <ApexBrand />
        </Box>
        <Box component="nav" aria-label="主导航" sx={{ px: 2, pt: 2, pb: 3 }}>
          {navigationGroups.map((group) => {
            const visibleItems = group.items.filter(
              (item) =>
                item.requiredPermission === undefined || hasPermission(item.requiredPermission),
            );

            return visibleItems.length > 0 ? (
              <Box key={group.label} sx={{ "& + &": { mt: 1.5 } }}>
                <Typography
                  variant="overline"
                  color="text.disabled"
                  sx={{ display: "block", px: 1.5, mb: 1, letterSpacing: "0.08em" }}
                >
                  {group.label}
                </Typography>
                <List disablePadding sx={{ display: "grid", gap: 0.5 }}>
                  {visibleItems.map((item) => (
                    <ListItemButton
                      key={item.to}
                      component={RouterLink}
                      to={item.to}
                      selected={location.pathname === item.to}
                      aria-current={location.pathname === item.to ? "page" : undefined}
                      sx={{
                        minHeight: 44,
                        borderRadius: 1,
                        color: "text.secondary",
                        "&:hover": { bgcolor: "grey.100", color: "text.primary" },
                        "&.Mui-selected": {
                          bgcolor: brandColors.primaryLighter,
                          color: "primary.dark",
                        },
                        "&.Mui-selected:hover": { bgcolor: brandColors.primaryLighter },
                      }}
                    >
                      <ListItemIcon sx={{ minWidth: 36, color: "inherit" }}>
                        {item.icon}
                      </ListItemIcon>
                      <ListItemText
                        primary={item.label}
                        primaryTypographyProps={{ fontWeight: 700 }}
                      />
                    </ListItemButton>
                  ))}
                </List>
              </Box>
            ) : null;
          })}
        </Box>
      </Box>

      <Box
        sx={{
          minWidth: 0,
          flex: 1,
          display: "flex",
          flexDirection: "column",
          bgcolor: "grey.200",
        }}
      >
        <Box
          component="header"
          sx={{
            minHeight: appLayout.appBarDesktopHeight,
            px: 3,
            display: "grid",
            gridTemplateColumns: "minmax(0, 1fr) auto",
            alignItems: "center",
            gap: 2,
            borderBottom: 1,
            borderColor: "divider",
            bgcolor: "background.paper",
            "@media (min-width: 1440px)": { px: 4 },
            "@media (min-width: 1920px)": { px: 5 },
          }}
        >
          <ButtonBase
            aria-label="全局搜索功能即将开放"
            onClick={handleGlobalSearch}
            sx={{
              width: "min(100%, 560px)",
              height: 42,
              justifySelf: "center",
              justifyContent: "flex-start",
              gap: 1.25,
              px: 1.75,
              border: 1,
              borderColor: "divider",
              borderRadius: 1,
              color: "text.secondary",
              bgcolor: "background.paper",
              "&:hover": { borderColor: "grey.400", bgcolor: "grey.50" },
            }}
          >
            <SearchOutlinedIcon fontSize="small" />
            <Typography variant="body2">搜索股票、页面或功能</Typography>
            <Box
              component="span"
              sx={{
                ml: "auto",
                px: 0.75,
                py: 0.25,
                border: 1,
                borderColor: "divider",
                borderRadius: 0.75,
                bgcolor: "grey.100",
                color: "text.disabled",
                fontSize: 11,
                lineHeight: 1.4,
              }}
            >
              ⌘ K
            </Box>
          </ButtonBase>
          <Stack direction="row" spacing={0.5} alignItems="center">
            <Tooltip title="通知功能即将开放">
              <span>
                <IconButton aria-label="通知功能即将开放" disabled>
                  <NotificationsNoneOutlinedIcon />
                </IconButton>
              </span>
            </Tooltip>
            <Tooltip title="帮助与系统提示">
              <IconButton aria-label="帮助与系统提示" onClick={handleHelp}>
                <HelpOutlineOutlinedIcon />
              </IconButton>
            </Tooltip>
            <Button
              aria-label="打开用户菜单"
              aria-controls={accountMenuAnchor === null ? undefined : "account-actions-menu"}
              aria-haspopup="menu"
              onClick={handleAccountMenuOpen}
              endIcon={<KeyboardArrowDownOutlinedIcon />}
              sx={{ minHeight: 48, color: "text.primary", textAlign: "left" }}
            >
              <Avatar sx={{ width: 36, height: 36, mr: 1, bgcolor: "primary.main", fontSize: 14 }}>
                {userInitial(user.displayName)}
              </Avatar>
              <Stack spacing={0} alignItems="flex-start">
                <Typography component="span" variant="body2" fontWeight={700}>
                  {user.displayName}
                </Typography>
                <Typography component="span" variant="caption" color="text.secondary">
                  {userRoleLabel(user.role)}
                </Typography>
              </Stack>
            </Button>
          </Stack>
          <Menu
            id="account-actions-menu"
            anchorEl={accountMenuAnchor}
            open={accountMenuAnchor !== null}
            onClose={handleAccountMenuClose}
            slotProps={{ list: { "aria-label": "用户操作" } }}
          >
            <MenuItem onClick={handleLogout}>
              <ListItemIcon>
                <LogoutOutlinedIcon fontSize="small" />
              </ListItemIcon>
              退出登录
            </MenuItem>
          </Menu>
        </Box>
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
