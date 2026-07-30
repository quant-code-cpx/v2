import { RefreshRounded as RefreshRoundedIcon } from "@mui/icons-material";
import { Alert, Box, Button, Skeleton, Stack, Typography } from "@mui/material";

/** 远程 typed dataset 的明确非数据状态。 */
type MarketDataStateKind =
  | "loading"
  | "error"
  | "empty"
  | "source-unavailable"
  | "currently-unsupported";

/** 数据状态视图所需的业务文案与可恢复动作。 */
interface MarketDataStateViewProps {
  kind: MarketDataStateKind;
  title: string;
  description: string;
  onRetry?: () => void;
  minHeight?: number;
}

/** 在列表和详情中统一呈现加载、空 publication、空记录与可恢复错误。 */
export function MarketDataStateView({
  kind,
  title,
  description,
  onRetry,
  minHeight = 176,
}: MarketDataStateViewProps) {
  if (kind === "loading") {
    return (
      <Stack aria-label={title} spacing={1.5} sx={{ minHeight, justifyContent: "center" }}>
        <Skeleton variant="rounded" height={36} />
        <Skeleton variant="rounded" height={36} />
        <Skeleton variant="rounded" height={36} />
      </Stack>
    );
  }

  if (kind === "error") {
    return (
      <Alert
        severity="error"
        action={
          onRetry === undefined ? null : (
            <Button
              color="inherit"
              size="small"
              startIcon={<RefreshRoundedIcon />}
              onClick={onRetry}
            >
              重试
            </Button>
          )
        }
      >
        <Typography fontWeight={700}>{title}</Typography>
        <Typography variant="body2">{description}</Typography>
      </Alert>
    );
  }

  return (
    <Box
      role="status"
      sx={{
        minHeight,
        display: "grid",
        placeItems: "center",
        borderRadius: 2,
        bgcolor: "grey.100",
        px: 3,
        py: 4,
        textAlign: "center",
      }}
    >
      <Box>
        <Typography fontWeight={700}>{title}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
          {description}
        </Typography>
      </Box>
    </Box>
  );
}
