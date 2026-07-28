import {
  HelpOutlineOutlined as HelpOutlineOutlinedIcon,
  KeyboardArrowDownOutlined as KeyboardArrowDownOutlinedIcon,
  LogoutOutlined as LogoutOutlinedIcon,
  NotificationsNoneOutlined as NotificationsNoneOutlinedIcon,
  PersonOutlineOutlined as PersonOutlineOutlinedIcon,
  SearchOutlined as SearchOutlinedIcon,
} from "@mui/icons-material";
import {
  Avatar,
  Box,
  Button,
  ButtonBase,
  IconButton,
  ListItemIcon,
  Menu,
  MenuItem,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";

import type { CurrentUser } from "../../../types/access";
import { appLayout } from "../../../styles/design-tokens";
import { userRoleLabel } from "../../../utils/user-presentation";
import { useAppHeaderActions } from "../hooks/useAppHeaderActions";

/** 描述应用头部需要展示的已认证身份。 */
interface AppHeaderProps {
  user: CurrentUser;
}

/** 渲染桌面应用头部、预留搜索与账号菜单。 */
export function AppHeader({ user }: AppHeaderProps) {
  const actions = useAppHeaderActions();

  return (
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
        onClick={actions.handleGlobalSearch}
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
          <IconButton aria-label="帮助与系统提示" onClick={actions.handleHelp}>
            <HelpOutlineOutlinedIcon />
          </IconButton>
        </Tooltip>
        <Button
          aria-label="打开用户菜单"
          aria-controls={actions.accountMenuAnchor === null ? undefined : "account-actions-menu"}
          aria-haspopup="menu"
          onClick={actions.handleAccountMenuOpen}
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
        anchorEl={actions.accountMenuAnchor}
        open={actions.accountMenuAnchor !== null}
        onClose={actions.handleAccountMenuClose}
        slotProps={{ list: { "aria-label": "用户操作" } }}
      >
        <MenuItem onClick={actions.handleAccount}>
          <ListItemIcon>
            <PersonOutlineOutlinedIcon fontSize="small" />
          </ListItemIcon>
          我的账户
        </MenuItem>
        <MenuItem onClick={actions.handleLogout}>
          <ListItemIcon>
            <LogoutOutlinedIcon fontSize="small" />
          </ListItemIcon>
          退出登录
        </MenuItem>
      </Menu>
    </Box>
  );
}

/** 返回用于非敏感登录身份标记的首字母。 */
function userInitial(displayName: string): string {
  return displayName.trim().slice(0, 1).toUpperCase() || "A";
}
