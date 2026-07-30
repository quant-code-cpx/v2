import { useMemo } from "react";
import { Box } from "@mui/material";
import type { EChartsOption } from "echarts";

import { useECharts } from "../../../hooks/useEcharts";
import { useChartVisualTokens } from "../../../styles/chart-tokens";
import type { EquityValuationPage } from "../../../types/equity-market";

/** 从真实供应商估值观察构建 ECharts PE TTM 趋势。 */
export function ValuationChart({ page }: { page: EquityValuationPage }) {
  const tokens = useChartVisualTokens();
  const option = useMemo<EChartsOption>(() => {
    const rows = page.items
      .filter(
        /** 当前图只选择 PE TTM，避免把不同单位指标混入同一坐标轴。 */
        (item) => item.metricCode === "pe_ttm",
      )
      .map(
        /** decimal string 仅在进入图表引擎时转换为 number。 */
        (item) => [item.observationDate, Number(item.value)],
      );

    return {
      aria: {
        enabled: true,
        description: "历史 PE TTM 供应商观察折线图，数据并非官方最终值。",
      },
      backgroundColor: tokens.background,
      dataset: { dimensions: ["date", "value"], source: rows },
      grid: { top: 28, right: 20, bottom: 36, left: 56 },
      tooltip: { trigger: "axis", valueFormatter: (value) => `${String(value)} 倍` },
      xAxis: {
        type: "category",
        axisLabel: { color: tokens.textSecondary },
        axisLine: { lineStyle: { color: tokens.gridLine } },
      },
      yAxis: {
        type: "value",
        scale: true,
        name: "倍",
        nameTextStyle: { color: tokens.textSecondary },
        axisLabel: { color: tokens.textSecondary },
        splitLine: { lineStyle: { color: tokens.gridLine, type: "dashed" } },
      },
      series: [
        {
          type: "line",
          name: "PE TTM",
          encode: { x: "date", y: "value" },
          color: tokens.series[0],
          showSymbol: false,
          smooth: false,
        },
      ],
    };
  }, [page.items, tokens]);
  const containerRef = useECharts(option);

  return (
    <Box
      ref={containerRef}
      role="img"
      aria-label="历史 PE TTM 供应商观察趋势"
      sx={{ width: "100%", minHeight: 360 }}
    />
  );
}
