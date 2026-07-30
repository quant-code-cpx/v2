import { Box, Breadcrumbs, Link, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";

/** 描述互联互通三个页面共享的紧凑标题区。 */
interface StockConnectPageHeaderProps {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
  breadcrumb?: string;
}

/** 渲染市场数据上下文、主问题和桌面筛选动作，不使用营销 Hero。 */
export function StockConnectPageHeader({
  eyebrow,
  title,
  description,
  actions,
  breadcrumb,
}: StockConnectPageHeaderProps) {
  return (
    <Stack spacing={2}>
      {breadcrumb === undefined ? null : (
        <Breadcrumbs aria-label="互联互通导航">
          <Link component={RouterLink} to="/market/stock-connect" underline="hover">
            互联互通
          </Link>
          <Typography color="text.primary">{breadcrumb}</Typography>
        </Breadcrumbs>
      )}
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={3}>
        <Box>
          <Typography variant="overline" color="primary.main" sx={{ letterSpacing: "0.08em" }}>
            {eyebrow}
          </Typography>
          <Typography component="h1" variant="h3">
            {title}
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5, maxWidth: 760 }}>
            {description}
          </Typography>
        </Box>
        {actions}
      </Stack>
    </Stack>
  );
}
