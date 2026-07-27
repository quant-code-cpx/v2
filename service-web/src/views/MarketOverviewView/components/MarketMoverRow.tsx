import { Box, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import type { MarketMover } from "../../../types/market";
import { compactMarketNumberFormatter, marketNumberFormatter } from "../utils/market-formatters";
import { ChangeValue } from "./ChangeValue";

/** 渲染可导航的活跃标的行，展示紧凑成交额与有符号涨跌。 */
export function MarketMoverRow({ mover }: { mover: MarketMover }) {
  return (
    <Box
      component={RouterLink}
      to={`/instruments/${mover.symbol}`}
      sx={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) auto",
        gap: 2,
        alignItems: "center",
        py: 1.25,
        color: "inherit",
        textDecoration: "none",
        "&:hover": { color: "primary.main" },
      }}
    >
      <Box>
        <Typography fontWeight={700}>{mover.name}</Typography>
        <Typography variant="caption" color="text.secondary">
          {mover.symbol} · 成交额 {compactMarketNumberFormatter.format(mover.turnover)}
        </Typography>
      </Box>
      <Box textAlign="right">
        <Typography fontWeight={700}>{marketNumberFormatter.format(mover.price)}</Typography>
        <ChangeValue value={mover.changePercent} />
      </Box>
    </Box>
  );
}
