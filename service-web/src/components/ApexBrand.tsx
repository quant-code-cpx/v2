import { Box, Stack, Typography } from "@mui/material";

import { brandColors } from "../styles/design-tokens";

/** 渲染不依赖远程图片的 Apex 市场信号标志与产品字标。 */
export function ApexBrand() {
  return (
    <Stack direction="row" spacing={1.25} alignItems="center">
      <Box
        component="svg"
        viewBox="0 0 28 32"
        role="img"
        aria-label="Apex 标志"
        sx={{ width: 28, height: 32, flexShrink: 0 }}
      >
        <rect
          x="10"
          y="2"
          width="5"
          height="27"
          rx="2.5"
          fill={brandColors.primary}
          transform="rotate(-28 12.5 15.5)"
        />
        <rect
          x="5"
          y="12"
          width="19"
          height="9"
          rx="4.5"
          fill={brandColors.secondary}
          transform="rotate(28 14.5 16.5)"
        />
      </Box>
      <Typography
        component="span"
        variant="subtitle1"
        sx={{
          color: "text.primary",
          fontWeight: 800,
          letterSpacing: "-0.015em",
          whiteSpace: "nowrap",
        }}
      >
        <Box component="span" sx={{ color: "primary.main" }}>
          Apex
        </Box>
        数据智能分析平台
      </Typography>
    </Stack>
  );
}
