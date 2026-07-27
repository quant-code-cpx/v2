import { lazy, Suspense } from "react";
import { ArrowOutwardRounded as ArrowOutwardRoundedIcon } from "@mui/icons-material";
import { Box, Card, CardContent, Chip, Divider, Skeleton, Stack, Typography } from "@mui/material";
import { useParams } from "react-router-dom";

import { demoAnalysisSeries, demoCandles } from "../../mocks/instrument-analysis";

/** 将 KLineChart 引擎面板从初始路由 Bundle 中拆出。 */
const KlinePanel = lazy(async () => {
  const { KlinePanel: Component } = await import("./components/KlinePanel");

  return { default: Component };
});

/** 标的分析页需要时才加载 ECharts 分析面板。 */
const AnalysisChart = lazy(async () => {
  const { AnalysisChart: Component } = await import("./components/AnalysisChart");

  return { default: Component };
});

/** 可视化 Bundle 懒加载期间保留图表几何。 */
function ChartSkeleton({ height }: { height: number }) {
  return <Skeleton variant="rounded" height={height} aria-label="正在载入图表" />;
}

/** 使用受控 fixture 渲染标的分析，并分离 K 线与分析图引擎。 */
export function InstrumentAnalysisView() {
  const { symbol = "600519" } = useParams();

  return (
    <Stack spacing={3}>
      <Box>
        <Stack direction="row" justifyContent="space-between" spacing={1}>
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
