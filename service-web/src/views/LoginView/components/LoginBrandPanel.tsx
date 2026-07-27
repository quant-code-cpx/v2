import { Box, Stack, Typography } from "@mui/material";

/** 渲染登录页独立品牌说明区，与表单状态完全隔离。 */
export function LoginBrandPanel() {
  return (
    <Box
      component="section"
      aria-label="平台介绍"
      sx={{
        px: 12,
        py: 10,
        display: "grid",
        gridTemplateRows: "auto 1fr",
        bgcolor: "grey.100",
      }}
    >
      <Stack direction="row" spacing={1.5} alignItems="center">
        <Box
          aria-hidden="true"
          sx={{
            position: "relative",
            width: 28,
            height: 28,
            flex: "0 0 auto",
          }}
        >
          <Box
            sx={{
              position: "absolute",
              top: 2,
              left: 3,
              width: 13,
              height: 13,
              borderRadius: "50%",
              bgcolor: "primary.main",
            }}
          />
          <Box
            sx={{
              position: "absolute",
              right: 3,
              bottom: 2,
              width: 14,
              height: 14,
              borderRadius: "50%",
              bgcolor: "secondary.main",
            }}
          />
        </Box>
        <Typography variant="h6" fontWeight={800}>
          Apex数据智能分析平台
        </Typography>
      </Stack>

      <Box sx={{ maxWidth: 480, alignSelf: "center" }}>
        <Typography component="h1" variant="h1">
          看见数据，
          <br />
          读懂市场。
        </Typography>
        <Typography color="text.secondary" sx={{ mt: 3 }}>
          登录后进入股票与市场数据分析平台。后续 AI 研究助手将帮助发现线索，判断仍由你完成。
        </Typography>
      </Box>
    </Box>
  );
}
