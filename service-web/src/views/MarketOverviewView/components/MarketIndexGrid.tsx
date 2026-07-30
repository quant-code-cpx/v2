import { Box, Card, CardContent, Stack, Typography } from "@mui/material";

import { MarketDirectionalValue } from "../../../components/MarketDirectionalValue";
import type { MarketOverview } from "../../../types/market";
import { formatMarketDecimal, formatSourceDecimal } from "../../../utils/market-formatters";

/** 描述市场完整包中的单个指数元素。 */
type MarketIndex = MarketOverview["indices"][number];

/** 渲染一个主要指数的点位、涨跌和来源直报日内区间。 */
function MarketIndexCard({ index }: { index: MarketIndex }) {
  return (
    <Card>
      <CardContent>
        <Typography variant="body2" color="text.secondary">
          {index.name} · {index.indexId}
        </Typography>
        <Typography
          variant="h5"
          sx={{ mt: 1, fontVariantNumeric: "tabular-nums", letterSpacing: "-0.02em" }}
        >
          {formatMarketDecimal(index.point, 3, 3)}
        </Typography>
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mt: 0.5 }}>
          <Typography
            variant="body2"
            color={
              Number(index.change) > 0
                ? "error.main"
                : Number(index.change) < 0
                  ? "success.main"
                  : "text.secondary"
            }
            fontWeight={700}
            sx={{ fontVariantNumeric: "tabular-nums" }}
          >
            {Number(index.change) > 0 ? "+" : ""}
            {formatMarketDecimal(index.change, 3, 3)}
          </Typography>
          <MarketDirectionalValue value={index.changePercent} variant="compact" />
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.25 }}>
          日内 {formatMarketDecimal(index.low, 2)}–{formatMarketDecimal(index.high, 2)}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
          成交量 {formatSourceDecimal(index.volume, " 手", 0)}
        </Typography>
      </CardContent>
    </Card>
  );
}

/** 渲染冻结为四项的主要指数带，保持完整包顺序。 */
export function MarketIndexGrid({ indices }: { indices: MarketOverview["indices"] }) {
  return (
    <Box
      component="section"
      aria-label="主要指数"
      sx={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 2 }}
    >
      {indices.map(
        /** 每项 identity 来自服务端固定指数集合，不使用数组位置作为身份。 */
        (index) => (
          <MarketIndexCard key={index.indexId} index={index} />
        ),
      )}
    </Box>
  );
}
