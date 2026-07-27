import { Box, CircularProgress } from "@mui/material";

/** 描述全屏加载状态的可访问名称。 */
interface RouteLoadingViewProps {
  label?: string;
}

/** 路由 lazy 与 loader 首次完成前渲染稳定的桌面加载界面。 */
export function RouteLoadingView({ label = "正在加载页面" }: RouteLoadingViewProps) {
  return (
    <Box
      role="status"
      aria-label={label}
      sx={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        bgcolor: "background.default",
      }}
    >
      <CircularProgress size={28} />
    </Box>
  );
}
