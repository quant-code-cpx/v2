import { useMemo } from "react";
import { Box } from "@mui/material";
import type { EChartsOption } from "echarts";

import { useECharts } from "../hooks/useEcharts";
import { useChartVisualTokens } from "../styles/chart-tokens";
import type { ChartVisualTokens } from "../styles/chart-tokens";

/** 描述一个已发布板块强弱点。 */
export interface MarketStrengthPoint {
  name: string;
  changePercent: string;
}

/** 为 ECharts 百分比轴追加明确单位。 */
function formatPercentAxis(value: number): string {
  return `${value}%`;
}

/** 构建红涨绿跌的板块强弱横向条形图。 */
function buildStrengthOption(
  points: readonly MarketStrengthPoint[],
  tokens: ChartVisualTokens,
): EChartsOption {
  return {
    aria: { enabled: true },
    backgroundColor: tokens.background,
    grid: { top: 12, right: 28, bottom: 24, left: 96, containLabel: false },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "value",
      axisLabel: { color: tokens.textSecondary, formatter: formatPercentAxis },
      axisLine: { lineStyle: { color: tokens.gridLine } },
      splitLine: { lineStyle: { color: tokens.gridLine, type: "dashed" } },
    },
    yAxis: {
      type: "category",
      data: points.map(
        /** 保持 API 排名顺序，只向坐标轴投影名称。 */
        (point) => point.name,
      ),
      axisLabel: { color: tokens.textSecondary },
      axisLine: { lineStyle: { color: tokens.gridLine } },
    },
    series: [
      {
        name: "涨跌幅",
        type: "bar",
        barMaxWidth: 18,
        data: points.map(
          /** 每根柱按中国市场方向语义着色，并保留数值正负号。 */
          (point) => ({
            value: Number(point.changePercent),
            itemStyle: {
              color:
                Number(point.changePercent) > 0
                  ? tokens.positive
                  : Number(point.changePercent) < 0
                    ? tokens.negative
                    : tokens.neutral,
            },
          }),
        ),
      },
    ],
  };
}

/** 使用 ECharts 渲染非 K 线强弱分析，并把高频 tooltip 状态留在引擎内部。 */
export function MarketStrengthChart({ points }: { points: readonly MarketStrengthPoint[] }) {
  const tokens = useChartVisualTokens();
  const option = useMemo(
    /** 仅在真实排名或主题 Token 变化时重建图表配置。 */
    () => buildStrengthOption(points, tokens),
    [points, tokens],
  );
  const containerRef = useECharts(option);

  return (
    <Box
      ref={containerRef}
      role="img"
      aria-label="板块涨跌幅强弱图"
      sx={{ minHeight: 320, width: "100%" }}
    />
  );
}
