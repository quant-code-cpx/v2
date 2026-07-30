import { Box, Card, CardContent, Typography } from "@mui/material";

import type { EtfDailyBarValues, EtfNavValues } from "../../../types/etf";
import { etfNavFinalityLabel } from "../../../utils/etf-presentation";

/** 最新价格与单位 NAV 摘要只接受来源原值及独立状态文案。 */
interface EtfLatestValuesProps {
  latestBar: EtfDailyBarValues | undefined;
  latestNav: EtfNavValues | undefined;
  barFallback: string;
  navFallback: string;
}

/** 并列展示最新收盘价与单位 NAV，不计算或展示折溢价。 */
export function EtfLatestValues({
  latestBar,
  latestNav,
  barFallback,
  navFallback,
}: EtfLatestValuesProps) {
  return (
    <Box
      component="section"
      aria-label="最新价格与单位净值"
      sx={{
        display: "grid",
        gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
        gap: 3,
      }}
    >
      <Card>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="body2" color="text.secondary">
            最新收盘价
          </Typography>
          {latestBar === undefined ? (
            <Typography sx={{ mt: 1.5, color: "text.secondary" }}>{barFallback}</Typography>
          ) : (
            <>
              <Typography
                variant="h4"
                sx={{ mt: 1, fontVariantNumeric: "tabular-nums", fontWeight: 700 }}
              >
                {latestBar.close} {latestBar.currency}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {latestBar.tradeDate} · 未复权 · 成交量 {latestBar.volume} {latestBar.volumeUnit}
              </Typography>
            </>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="body2" color="text.secondary">
            最新单位 NAV
          </Typography>
          {latestNav === undefined ? (
            <Typography sx={{ mt: 1.5, color: "text.secondary" }}>{navFallback}</Typography>
          ) : (
            <>
              <Typography
                variant="h4"
                sx={{ mt: 1, fontVariantNumeric: "tabular-nums", fontWeight: 700 }}
              >
                {latestNav.nav} {latestNav.currency}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {latestNav.navDate} · 单位净值 · {etfNavFinalityLabel(latestNav.finality)}
              </Typography>
            </>
          )}
        </CardContent>
      </Card>
    </Box>
  );
}
