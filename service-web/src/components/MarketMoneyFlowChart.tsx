import { useMemo } from "react";
import { Box } from "@mui/material";
import type { EChartsOption } from "echarts";

import { useECharts } from "../hooks/useEcharts";
import { useChartVisualTokens } from "../styles/chart-tokens";
import type { ChartVisualTokens } from "../styles/chart-tokens";

/** 描述一条已发布板块来源资金流排行。 */
export interface MarketMoneyFlowPoint {
  name: string;
  netAmountCny: string;
}

/** 以亿元格式化人民币净额坐标轴。 */
function formatCnyAxis(value: number): string {
  return `${(value / 100_000_000).toFixed(0)}亿`;
}

/** 构建中国市场红流入、绿流出的横向资金流图。 */
function buildMoneyFlowOption(
  points: readonly MarketMoneyFlowPoint[],
  tokens: ChartVisualTokens,
): EChartsOption {
  return {
    aria: { enabled: true },
    backgroundColor: tokens.background,
    grid: { top: 12, right: 28, bottom: 24, left: 96, containLabel: false },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "value",
      axisLabel: { color: tokens.textSecondary, formatter: formatCnyAxis },
      axisLine: { lineStyle: { color: tokens.gridLine } },
      splitLine: { lineStyle: { color: tokens.gridLine, type: "dashed" } },
    },
    yAxis: {
      type: "category",
      data: points.map(
        /** 保持 API 排名顺序，只投影板块名称。 */
        (point) => point.name,
      ),
      axisLabel: { color: tokens.textSecondary },
      axisLine: { lineStyle: { color: tokens.gridLine } },
    },
    series: [
      {
        name: "来源净额",
        type: "bar",
        barMaxWidth: 18,
        data: points.map(
          /** 净流入用红色、净流出用绿色，并保留数值正负。 */
          (point) => ({
            value: Number(point.netAmountCny),
            itemStyle: {
              color:
                Number(point.netAmountCny) > 0
                  ? tokens.positive
                  : Number(point.netAmountCny) < 0
                    ? tokens.negative
                    : tokens.neutral,
            },
          }),
        ),
      },
    ],
  };
}

/** 使用 ECharts 渲染供应商方法学内的板块资金流排行。 */
export function MarketMoneyFlowChart({ points }: { points: readonly MarketMoneyFlowPoint[] }) {
  const tokens = useChartVisualTokens();
  const option = useMemo(
    /** 仅在真实排名或主题 Token 变化时重建图表配置。 */
    () => buildMoneyFlowOption(points, tokens),
    [points, tokens],
  );
  const containerRef = useECharts(option);

  return (
    <Box
      ref={containerRef}
      role="img"
      aria-label="板块来源资金流排行图"
      sx={{ minHeight: 340, width: "100%" }}
    />
  );
}
