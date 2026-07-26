import { ErrorOutline as ErrorOutlineIcon } from "@mui/icons-material";
import { Alert, Box, Button, Stack, Typography } from "@mui/material";
import { isRouteErrorResponse, Link as RouterLink, useRouteError } from "react-router-dom";

export function RouteErrorView() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : "页面暂时不可用，请稍后重试。";

  return (
    <Box sx={{ minHeight: "100dvh", display: "grid", placeItems: "center", p: 3 }}>
      <Stack spacing={2} alignItems="flex-start" sx={{ width: "min(100%, 480px)" }}>
        <ErrorOutlineIcon color="error" sx={{ fontSize: 42 }} />
        <Typography variant="h4">无法加载此页面</Typography>
        <Alert severity="error" sx={{ width: "100%" }}>
          {message}
        </Alert>
        <Button component={RouterLink} to="/" variant="contained">
          返回市场概览
        </Button>
      </Stack>
    </Box>
  );
}
