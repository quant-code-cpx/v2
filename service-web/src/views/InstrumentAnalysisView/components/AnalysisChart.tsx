import { useMemo } from "react";
import { Box } from "@mui/material";
import type { EChartsOption } from "echarts";

import { useECharts } from "../../../hooks/useEcharts";
import { useChartVisualTokens } from "../../../styles/chart-tokens";
import type { ChartVisualTokens } from "../../../styles/chart-tokens";

export interface AnalysisPoint {
  date: string;
  close: number;
  benchmark: number;
}

/** 将分析点转换为 ECharts 可识别的不可变数据行。 */
function toAnalysisRow(point: AnalysisPoint): [string, number, number] {
  return [point.date, point.close, point.benchmark];
}

/** 为非 K 线相对表现分析构建 ECharts 配置。 */
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
      source: data.map(toAnalysisRow),
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

/** 渲染随容器变化的 ECharts 收盘价与基准对比图。 */
export function AnalysisChart({ data }: { data: readonly AnalysisPoint[] }) {
  const tokens = useChartVisualTokens();
  // 仅在视觉 Token 或不可变序列变化时重建配置。
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
