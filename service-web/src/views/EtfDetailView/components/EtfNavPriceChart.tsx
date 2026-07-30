import { useMemo } from "react";
import { Box } from "@mui/material";
import type { EChartsOption } from "echarts";

import { useECharts } from "../../../hooks/useEcharts";
import { useChartVisualTokens } from "../../../styles/chart-tokens";
import type { ChartVisualTokens } from "../../../styles/chart-tokens";
import type { EtfNavPricePoint } from "../utils/etf-detail";

/** 将价格与单位 NAV 原值行转换为 ECharts dataset。 */
function toDatasetRow(point: EtfNavPricePoint): [string, number | null, number | null] {
  return [point.date, point.close, point.nav];
}

/** 构造双轴原值比较图，不计算折溢价、收益率或缺失日插值。 */
export function buildEtfNavPriceOption(
  points: readonly EtfNavPricePoint[],
  tokens: ChartVisualTokens,
): EChartsOption {
  return {
    aria: { enabled: true },
    backgroundColor: tokens.background,
    color: [tokens.series[0] ?? tokens.textPrimary, tokens.series[2] ?? tokens.textSecondary],
    dataset: {
      dimensions: ["date", "close", "nav"],
      source: points.map(toDatasetRow),
    },
    grid: { top: 52, right: 64, bottom: 36, left: 64 },
    legend: {
      top: 12,
      textStyle: { color: tokens.textSecondary },
    },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      boundaryGap: false,
      axisLabel: { color: tokens.textSecondary },
      axisLine: { lineStyle: { color: tokens.gridLine } },
    },
    yAxis: [
      {
        type: "value",
        name: "收盘价",
        scale: true,
        axisLabel: { color: tokens.textSecondary },
        splitLine: { lineStyle: { color: tokens.gridLine, type: "dashed" } },
      },
      {
        type: "value",
        name: "单位 NAV",
        scale: true,
        axisLabel: { color: tokens.textSecondary },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        type: "line",
        name: "收盘价",
        yAxisIndex: 0,
        encode: { x: "date", y: "close" },
        showSymbol: false,
        connectNulls: false,
      },
      {
        type: "line",
        name: "单位 NAV",
        yAxisIndex: 1,
        encode: { x: "date", y: "nav" },
        showSymbol: false,
        connectNulls: false,
      },
    ],
  };
}

/** 使用 ECharts 渲染 ETF 收盘价与单位 NAV 原值比较。 */
export function EtfNavPriceChart({ points }: { points: readonly EtfNavPricePoint[] }) {
  const tokens = useChartVisualTokens();
  const option = useMemo(
    /** 只有来源点或视觉 Token 变化时才重建图表配置。 */
    () => buildEtfNavPriceOption(points, tokens),
    [points, tokens],
  );
  const containerRef = useECharts(option);

  return (
    <Box
      ref={containerRef}
      role="img"
      aria-label="ETF 收盘价与单位 NAV 原值比较图"
      sx={{ minHeight: 360, width: "100%" }}
    />
  );
}
