import {
  ErrorOutlineRounded as ErrorOutlineRoundedIcon,
  HourglassEmptyRounded as HourglassEmptyRoundedIcon,
  InfoOutlined as InfoOutlinedIcon,
  SyncProblemRounded as SyncProblemRoundedIcon,
} from "@mui/icons-material";
import { Box, Button, Stack, Typography } from "@mui/material";

/** 描述一个远端组件的可恢复状态，不用伪造数值填满卡片。 */
export interface MarketDataStateProps {
  variant: "empty" | "error" | "unavailable" | "stale";
  title: string;
  message: string;
  onRetry?: () => void;
  minHeight?: number;
}

/** 为局部空、错、旧和不可用状态保留稳定几何及明确恢复动作。 */
export function MarketDataState({
  variant,
  title,
  message,
  onRetry,
  minHeight = 180,
}: MarketDataStateProps) {
  const Icon =
    variant === "error"
      ? ErrorOutlineRoundedIcon
      : variant === "stale"
        ? SyncProblemRoundedIcon
        : variant === "empty"
          ? HourglassEmptyRoundedIcon
          : InfoOutlinedIcon;
  const color =
    variant === "error" ? "error.main" : variant === "stale" ? "warning.main" : "info.main";

  return (
    <Box
      role={variant === "error" ? "alert" : "status"}
      sx={{
        minHeight,
        display: "grid",
        placeItems: "center",
        px: 3,
        py: 2,
        border: 1,
        borderStyle: "dashed",
        borderColor: "divider",
        borderRadius: 2,
        bgcolor: "grey.100",
        textAlign: "center",
      }}
    >
      <Stack spacing={1} alignItems="center" maxWidth={520}>
        <Icon sx={{ color }} aria-hidden />
        <Typography fontWeight={700}>{title}</Typography>
        <Typography variant="body2" color="text.secondary">
          {message}
        </Typography>
        {onRetry === undefined ? null : (
          <Button size="small" variant="outlined" onClick={onRetry}>
            局部重试
          </Button>
        )}
      </Stack>
    </Box>
  );
}
