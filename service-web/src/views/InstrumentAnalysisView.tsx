import { lazy, Suspense } from "react";
import { ArrowOutwardRounded as ArrowOutwardRoundedIcon } from "@mui/icons-material";
import { Box, Card, CardContent, Chip, Divider, Skeleton, Stack, Typography } from "@mui/material";
import { useParams } from "react-router-dom";

import { demoAnalysisSeries, demoCandles } from "../mocks/instrument-analysis";

/** Code-split KLineChart engine panel away from market-overview initial route. */
const KlinePanel = lazy(async () => {
  const { KlinePanel: Component } = await import("../components/KlinePanel");

  return { default: Component };
});

/** Code-split ECharts analysis panel until instrument page needs it. */
const AnalysisChart = lazy(async () => {
  const { AnalysisChart: Component } = await import("../components/AnalysisChart");

  return { default: Component };
});

/** Reserve chart layout while a lazily imported visualization bundle loads. */
function ChartSkeleton({ height }: { height: number }) {
  return <Skeleton variant="rounded" height={height} aria-label="正在载入图表" />;
}

/** Render fixture-backed instrument analysis with separated K-line and analytical chart engines. */
export function InstrumentAnalysisView() {
  const { symbol = "600519" } = useParams();

  return (
    <Stack spacing={3}>
      <Box>
        <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" spacing={1}>
          <Box>
            <Typography variant="h4">贵州茅台</Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }}>
              {symbol} · 上海证券交易所 · Asia/Shanghai
            </Typography>
          </Box>
          <Chip
            icon={<ArrowOutwardRoundedIcon />}
            label="Fixture · 非实时"
            variant="outlined"
            sx={{ alignSelf: "flex-start" }}
          />
        </Stack>
      </Box>

      <Card>
        <CardContent>
          <Typography variant="h6">K 线与技术指标</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
            KLineChart 独占 Candle、MA 与 VOL；组件按需加载。
          </Typography>
          <Suspense fallback={<ChartSkeleton height={440} />}>
            <KlinePanel symbol={symbol} period={{ span: 1, type: "day" }} candles={demoCandles} />
          </Suspense>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Typography variant="h6">相对表现</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
            ECharts 独占非 K 线分析图；使用 dataset 与 Canvas renderer。
          </Typography>
          <Suspense fallback={<ChartSkeleton height={360} />}>
            <AnalysisChart data={demoAnalysisSeries} />
          </Suspense>
        </CardContent>
      </Card>

      <Divider />
      <Typography variant="body2" color="text.secondary">
        下一步：接入 OpenAPI Candle 分页、WebSocket 增量、指标选择与 overlay 持久化。
      </Typography>
    </Stack>
  );
}
