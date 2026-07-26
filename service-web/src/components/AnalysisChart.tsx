import { useMemo } from "react";
import { Box } from "@mui/material";
import type { EChartsOption } from "echarts";

import { useECharts } from "../hooks/useEcharts";
import { useChartVisualTokens } from "../styles/chart-tokens";
import type { ChartVisualTokens } from "../styles/chart-tokens";

export interface AnalysisPoint {
  date: string;
  close: number;
  benchmark: number;
}

/** Build ECharts-only option for non-candlestick relative-performance analysis. */
function buildPerformanceOption(
  data: readonly AnalysisPoint[],
  tokens: ChartVisualTokens,
): EChartsOption {
  return {
    aria: { enabled: true },
    backgroundColor: tokens.background,
    color: [...tokens.series],
    dataset: {
      dimensions: ["date", "close", "benchmark"],
      // Clone data so chart engine cannot retain or mutate caller-owned point objects.
      source: data.map((point) => ({ ...point })),
    },
    grid: { top: 48, right: 20, bottom: 32, left: 52 },
    legend: { top: 12, textStyle: { color: tokens.textSecondary } },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      axisLabel: { color: tokens.textSecondary },
      axisLine: { lineStyle: { color: tokens.gridLine } },
    },
    yAxis: {
      type: "value",
      scale: true,
      axisLabel: { color: tokens.textSecondary },
      splitLine: { lineStyle: { color: tokens.gridLine } },
    },
    series: [
      {
        type: "line",
        name: "收盘价",
        encode: { x: "date", y: "close" },
        showSymbol: false,
        smooth: true,
      },
      {
        type: "line",
        name: "基准",
        encode: { x: "date", y: "benchmark" },
        showSymbol: false,
        smooth: true,
      },
    ],
  };
}

/** Render container-resizing ECharts comparison of close price against benchmark. */
export function AnalysisChart({ data }: { data: readonly AnalysisPoint[] }) {
  const tokens = useChartVisualTokens();
  // Rebuild option only when visual tokens or immutable series changes.
  const option = useMemo(() => buildPerformanceOption(data, tokens), [data, tokens]);
  const containerRef = useECharts(option);

  return (
    <Box
      ref={containerRef}
      aria-label="收盘价与基准对比图"
      sx={{ minHeight: 360, width: "100%" }}
    />
  );
}
