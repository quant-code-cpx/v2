import { LockOutlined as LockOutlinedIcon } from "@mui/icons-material";
import { Box, Button, Stack, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

/** Render a shell-contained access-denied state without rendering restricted data. */
export function ForbiddenView() {
  return (
    <Box sx={{ minHeight: 420, display: "grid", placeItems: "center" }}>
      <Stack spacing={2} alignItems="center" textAlign="center">
        <LockOutlinedIcon color="disabled" sx={{ fontSize: 44 }} />
        <Typography variant="h4">无权访问此功能</Typography>
        <Typography color="text.secondary">当前账号没有用户管理权限。</Typography>
        <Button component={RouterLink} to="/" variant="contained">
          返回首页
        </Button>
      </Stack>
    </Box>
  );
}
