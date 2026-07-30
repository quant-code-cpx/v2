import { Chip, Stack, Typography } from "@mui/material";

import { marketColors } from "../../../styles/design-tokens";
import type { StockConnectMoneyFact } from "../../../types/stock-connect";
import {
  formatStockConnectMoneyFact,
  formatStockConnectNetFact,
  stockConnectAvailabilityLabel,
  stockConnectMoneyDirection,
} from "../utils/stock-connect-presentation";

/** 描述普通金额与带方向净额两种明确展示变体。 */
interface MoneyFactProps {
  label: string;
  fact: StockConnectMoneyFact;
  variant: "amount" | "net";
}

/** 显示原币金额与字段可用性；净额始终带符号、文字和中国市场颜色。 */
export function MoneyFact({ label, fact, variant }: MoneyFactProps) {
  const direction =
    variant === "net" && fact.value !== null
      ? stockConnectMoneyDirection(fact.value.amount)
      : "flat";
  const valueColor =
    direction === "positive"
      ? marketColors.up
      : direction === "negative"
        ? marketColors.down
        : "text.primary";

  return (
    <Stack spacing={0.75}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Typography variant="caption" color="text.secondary">
          {label}
        </Typography>
        {fact.availability === "DERIVED" ? (
          <Chip size="small" color="info" label="同源派生" />
        ) : null}
      </Stack>
      <Typography
        variant="h6"
        sx={{
          color: valueColor,
          fontVariantNumeric: "tabular-nums",
          overflowWrap: "anywhere",
        }}
      >
        {variant === "net" ? formatStockConnectNetFact(fact) : formatStockConnectMoneyFact(fact)}
      </Typography>
      {fact.value === null ? (
        <Typography variant="caption" color="text.secondary">
          {stockConnectAvailabilityLabel(fact.availability)}
        </Typography>
      ) : null}
    </Stack>
  );
}
