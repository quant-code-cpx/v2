import {
  ArrowDownwardRounded as ArrowDownwardRoundedIcon,
  ArrowUpwardRounded as ArrowUpwardRoundedIcon,
  RemoveRounded as RemoveRoundedIcon,
} from "@mui/icons-material";
import { Stack, Typography } from "@mui/material";

import { formatMarketPercent } from "../utils/market-formatters";

/** 描述中国市场方向值的显示尺寸与可空语义。 */
interface MarketDirectionalValueProps {
  value: string | null | undefined;
  variant?: "body" | "compact";
  unavailableLabel?: string;
}

/** 用颜色、符号和图标共同表达中国市场红涨绿跌。 */
export function MarketDirectionalValue({
  value,
  variant = "body",
  unavailableLabel = "来源未报告",
}: MarketDirectionalValueProps) {
  if (value === null || value === undefined) {
    return (
      <Typography variant={variant === "compact" ? "caption" : "body2"} color="text.secondary">
        {unavailableLabel}
      </Typography>
    );
  }

  const numeric = Number(value);
  const direction = numeric > 0 ? "up" : numeric < 0 ? "down" : "flat";
  const color =
    direction === "up" ? "error.main" : direction === "down" ? "success.main" : "text.secondary";
  const DirectionIcon =
    direction === "up"
      ? ArrowUpwardRoundedIcon
      : direction === "down"
        ? ArrowDownwardRoundedIcon
        : RemoveRoundedIcon;

  return (
    <Stack direction="row" spacing={0.25} alignItems="center" color={color}>
      <DirectionIcon fontSize="inherit" aria-hidden />
      <Typography
        component="span"
        variant={variant === "compact" ? "caption" : "body2"}
        fontWeight={700}
        sx={{ fontVariantNumeric: "tabular-nums" }}
      >
        {formatMarketPercent(value)}
      </Typography>
    </Stack>
  );
}
