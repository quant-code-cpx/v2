import { Box, List, ListItemButton, ListItemIcon, ListItemText, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import type { Permission, UserRole } from "../../../types/access";
import { appLayout, brandColors } from "../../../styles/design-tokens";
import { ApexBrand } from "../../ApexBrand";
import { createNavigationGroups } from "../navigation";
import type { NavigationItem } from "../navigation";

/** 描述桌面侧栏所需的路由与权限信息。 */
interface AppSidebarProps {
  pathname: string;
  role: UserRole;
  hasPermission: (permission: Permission) => boolean;
}

/** 判断当前路径是否属于一个导航目标或其明确声明的子路由。 */
function navigationItemIsSelected(item: NavigationItem, pathname: string): boolean {
  return (
    pathname === item.to ||
    item.activePrefixes?.some(
      /** 只使用导航项显式声明的前缀，不做任意字符串包含匹配。 */
      (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
    ) === true
  );
}

/** 渲染固定桌面导航，并隐藏服务端未授予的入口。 */
export function AppSidebar({ pathname, role, hasPermission }: AppSidebarProps) {
  const navigationGroups = createNavigationGroups();

  return (
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
        {/* 按业务区域渲染稳定导航组。 */}
        {navigationGroups.map((group) => {
          const visibleItems = group.items.filter(
            /** 只保留无需权限或已获服务端授权的导航项。 */
            (item) =>
              (item.requiredPermission === undefined || hasPermission(item.requiredPermission)) &&
              (item.allowedRoles === undefined || item.allowedRoles.includes(role)),
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
                {/* 将每个可见目标渲染为可键盘访问的路由链接。 */}
                {visibleItems.map((item) => {
                  const selected = navigationItemIsSelected(item, pathname);

                  return (
                    <ListItemButton
                      key={item.to}
                      component={RouterLink}
                      to={item.to}
                      selected={selected}
                      aria-current={selected ? "page" : undefined}
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
                  );
                })}
              </List>
            </Box>
          ) : null;
        })}
      </Box>
    </Box>
  );
}
