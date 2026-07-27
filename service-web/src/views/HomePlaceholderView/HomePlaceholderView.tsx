import {
  HomeOutlined as HomeOutlinedIcon,
  InfoOutlined as InfoOutlinedIcon,
  ManageAccountsOutlined as ManageAccountsOutlinedIcon,
} from "@mui/icons-material";
import {
  Box,
  Breadcrumbs,
  Button,
  Card,
  CardContent,
  Chip,
  Stack,
  Typography,
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import { useAuth } from "../../components/AuthProvider";
import { brandColors } from "../../styles/design-tokens";

/** 在市场与 AI 工作区就绪前渲染受保护首页占位。 */
export function HomePlaceholderView() {
  const { hasPermission } = useAuth();

  return (
    <Stack spacing={3}>
      <Box>
        <Breadcrumbs aria-label="当前位置" separator="/" sx={{ mb: 0.75 }}>
          <Typography variant="body2" color="text.secondary">
            工作区
          </Typography>
          <Typography variant="body2" color="text.primary">
            首页
          </Typography>
        </Breadcrumbs>
        <Typography component="h1" variant="h3">
          首页
        </Typography>
        <Typography color="text.secondary" sx={{ mt: 0.5 }}>
          登录后的默认落点。
        </Typography>
      </Box>
      <Card>
        <CardContent
          sx={{
            minHeight: 520,
            display: "grid",
            placeItems: "center",
          }}
        >
          <Stack spacing={2.5} alignItems="center" sx={{ maxWidth: 680, textAlign: "center" }}>
            <Box
              sx={{
                width: 72,
                height: 72,
                display: "grid",
                placeItems: "center",
                borderRadius: 2,
                bgcolor: brandColors.primaryLighter,
                color: "primary.main",
              }}
            >
              <HomeOutlinedIcon sx={{ fontSize: 34 }} />
            </Box>
            <Box>
              <Typography variant="h3">首页能力建设中</Typography>
              <Typography color="text.secondary" sx={{ mt: 1.5, maxWidth: 620 }}>
                当前仅提供受保护路由与应用骨架。功能齐全后，此处将在同一 /
                路由替换为“今日市场”，不会使用个人资产 Hero 或虚构行情占位。
              </Typography>
            </Box>
            <Stack direction="row" spacing={1} alignItems="center">
              {hasPermission("users:read") ? (
                <Button
                  component={RouterLink}
                  to="/users"
                  variant="contained"
                  startIcon={<ManageAccountsOutlinedIcon />}
                >
                  进入用户管理
                </Button>
              ) : null}
              <Chip
                icon={<InfoOutlinedIcon />}
                label="临时页面 · 后续原位替换"
                variant="outlined"
                sx={{ bgcolor: "grey.50", borderColor: "transparent", color: "text.secondary" }}
              />
            </Stack>
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}
