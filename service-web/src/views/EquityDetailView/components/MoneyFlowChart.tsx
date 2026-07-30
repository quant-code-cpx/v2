import { useMemo } from "react";
import { Box } from "@mui/material";
import type { EChartsOption } from "echarts";

import { useECharts } from "../../../hooks/useEcharts";
import { useChartVisualTokens } from "../../../styles/chart-tokens";
import type { EquityMoneyFlowDailyPage } from "../../../types/equity-market";

/** 从真实日频资金流观察构建红入绿出、同时带正负轴的 ECharts 柱图。 */
export function MoneyFlowChart({ page }: { page: EquityMoneyFlowDailyPage }) {
  const tokens = useChartVisualTokens();
  const option = useMemo<EChartsOption>(() => {
    const rows = page.items.map(
      /** decimal string 只在图表边界转换，null 保持断点而不是数字零。 */
      (item) => [item.tradeDate, item.netAmount === null ? null : Number(item.netAmount)],
    );

    return {
      aria: {
        enabled: true,
        description: "日频主力资金净额柱图，正值为净流入，负值为净流出。",
      },
      backgroundColor: tokens.background,
      dataset: { dimensions: ["date", "netAmount"], source: rows },
      grid: { top: 28, right: 20, bottom: 36, left: 72 },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value) =>
          value === null ? "无值" : `${String(value)} ${page.amountUnit}`,
      },
      xAxis: {
        type: "category",
        axisLabel: { color: tokens.textSecondary },
        axisLine: { lineStyle: { color: tokens.gridLine } },
      },
      yAxis: {
        type: "value",
        name: page.amountUnit,
        nameTextStyle: { color: tokens.textSecondary },
        axisLabel: { color: tokens.textSecondary },
        splitLine: { lineStyle: { color: tokens.gridLine, type: "dashed" } },
      },
      series: [
        {
          type: "bar",
          name: "净流入 / 净流出",
          encode: { x: "date", y: "netAmount" },
          itemStyle: {
            /** 中国市场正向净流入使用红色、负向净流出使用绿色，坐标值同时保留正负号。 */
            color: (parameters) => {
              const row = parameters.value as [string, number | null];
              return (row[1] ?? 0) >= 0 ? tokens.positive : tokens.negative;
            },
          },
        },
      ],
    };
  }, [page, tokens]);
  const containerRef = useECharts(option);

  return (
    <Box
      ref={containerRef}
      role="img"
      aria-label="日频主力资金净流入与净流出"
      sx={{ width: "100%", minHeight: 360 }}
    />
  );
}
