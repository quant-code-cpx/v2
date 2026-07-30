import { RefreshRounded as RefreshRoundedIcon } from "@mui/icons-material";
import { Box, Button, Chip, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";

/** 描述市场页面标题区的 publication 上下文与刷新动作。 */
interface MarketPageHeaderProps {
  title: string;
  subtitle: string;
  status?: ReactNode;
  onRefresh?: () => void;
  refreshing?: boolean;
}

/** 统一市场页面标题、数据状态和手动条件刷新入口。 */
export function MarketPageHeader({
  title,
  subtitle,
  status,
  onRefresh,
  refreshing = false,
}: MarketPageHeaderProps) {
  return (
    <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={3}>
      <Box>
        <Typography variant="h4">{title}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
          {subtitle}
        </Typography>
      </Box>
      <Stack direction="row" spacing={1} alignItems="center">
        {status ?? <Chip size="small" variant="outlined" label="等待 publication" />}
        {onRefresh === undefined ? null : (
          <Button
            size="small"
            variant="outlined"
            startIcon={<RefreshRoundedIcon />}
            onClick={onRefresh}
            loading={refreshing}
          >
            刷新
          </Button>
        )}
      </Stack>
    </Stack>
  );
}
