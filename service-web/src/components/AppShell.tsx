import {
  DarkModeOutlined as DarkModeOutlinedIcon,
  InsightsOutlined as InsightsOutlinedIcon,
  LightModeOutlined as LightModeOutlinedIcon,
  SpaceDashboardOutlined as SpaceDashboardOutlinedIcon,
} from "@mui/icons-material";
import {
  AppBar,
  Box,
  Button,
  Container,
  IconButton,
  Stack,
  Toolbar,
  Tooltip,
  Typography,
} from "@mui/material";
import { Link as RouterLink, Outlet, useLocation } from "react-router-dom";

import { useColorMode } from "../styles/theme";

const navigationItems = [
  { label: "市场概览", to: "/", icon: <SpaceDashboardOutlinedIcon fontSize="small" /> },
  { label: "标的分析", to: "/instruments/600519", icon: <InsightsOutlinedIcon fontSize="small" /> },
] as const;

/** Render persistent application navigation, color-mode control, and nested route outlet. */
export function AppShell() {
  const { mode, toggleColorMode } = useColorMode();
  const location = useLocation();

  return (
    <Box sx={{ minHeight: "100dvh", bgcolor: "background.default" }}>
      <AppBar
        position="sticky"
        color="transparent"
        elevation={0}
        sx={{ borderBottom: 1, borderColor: "divider", backdropFilter: "blur(14px)" }}
      >
        <Container maxWidth="xl">
          <Toolbar disableGutters sx={{ gap: { xs: 1, md: 3 }, minHeight: 64 }}>
            <Typography
              component={RouterLink}
              to="/"
              variant="h6"
              color="text.primary"
              sx={{ textDecoration: "none", fontWeight: 800 }}
            >
              quant-v2
            </Typography>
            <Stack direction="row" spacing={0.5} sx={{ flex: 1, overflowX: "auto" }}>
              {/* Map route metadata into navigation controls while preserving selected state. */}
              {navigationItems.map((item) => (
                <Button
                  key={item.to}
                  component={RouterLink}
                  to={item.to}
                  startIcon={item.icon}
                  color={location.pathname === item.to ? "primary" : "inherit"}
                  variant={location.pathname === item.to ? "contained" : "text"}
                  sx={{ flexShrink: 0 }}
                >
                  {item.label}
                </Button>
              ))}
            </Stack>
            <Tooltip title={mode === "dark" ? "切换浅色模式" : "切换深色模式"}>
              <IconButton color="inherit" onClick={toggleColorMode} aria-label="切换颜色模式">
                {mode === "dark" ? <LightModeOutlinedIcon /> : <DarkModeOutlinedIcon />}
              </IconButton>
            </Tooltip>
          </Toolbar>
        </Container>
      </AppBar>
      <Container component="main" maxWidth="xl" sx={{ py: { xs: 3, md: 4 } }}>
        <Outlet />
      </Container>
    </Box>
  );
}
