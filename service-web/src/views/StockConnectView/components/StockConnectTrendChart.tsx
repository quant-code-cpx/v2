import { Alert, Box, Stack, Typography } from "@mui/material";
import { useMemo } from "react";
import type { EChartsOption } from "echarts";

import { useECharts } from "../../../hooks/useEcharts";
import { useChartVisualTokens } from "../../../styles/chart-tokens";
import type { StockConnectChannelCode, StockConnectTrendPoint } from "../../../types/stock-connect";
import {
  formatStockConnectDecimal,
  formatStockConnectMoneyFact,
  formatStockConnectNetFact,
  stockConnectChannelLabel,
  stockConnectMoneyDirection,
} from "../utils/stock-connect-presentation";

/** 描述一条通道的原币日终趋势。 */
interface StockConnectTrendChartProps {
  channel: StockConnectChannelCode;
  points: readonly StockConnectTrendPoint[];
}

/** 描述已通过单币种和可绘制金额校验的趋势画布。 */
interface StockConnectTrendCanvasProps {
  channel: StockConnectChannelCode;
  points: readonly StockConnectTrendPoint[];
  currency: string;
}

/** 将逐点表保留在可访问树中，同时避免与图形重复占用桌面版面。 */
const visuallyHiddenTrendTableSx = {
  position: "absolute",
  width: "1px",
  height: "1px",
  padding: 0,
  margin: "-1px",
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
} as const;

/** 将趋势点转换为 ECharts 数据行，金额保持公开合同十进制字符串并保留点级 publication。 */
function toTrendRow(point: StockConnectTrendPoint): [string, string | null, string | null, string] {
  return [
    point.tradeDate,
    point.stats.turnoverAmount.value?.amount ?? null,
    point.stats.netBuyAmount.value?.amount ?? null,
    point.dataVersion,
  ];
}

/** 把 ECharts tooltip 值恢复为可安全展示的十进制文本。 */
function normalizeTrendTooltipAmount(value: unknown): string | null {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }

  return null;
}

/** 格式化趋势成交额，明确保持中性成交事实。 */
function formatTrendTurnoverTooltip(value: unknown, currency: string): string {
  const amount = normalizeTrendTooltipAmount(value);
  if (amount === null) {
    return "— 未披露";
  }

  const formatted = /^-?[0-9]+(?:\.[0-9]+)?$/u.test(amount)
    ? formatStockConnectDecimal(amount)
    : amount;
  return `${currency} ${formatted} · 成交额`;
}

/** 格式化趋势净额，始终同时呈现正负符号与净买入、净卖出文字。 */
function formatTrendNetTooltip(value: unknown, currency: string): string {
  const amount = normalizeTrendTooltipAmount(value);
  if (amount === null) {
    return "— 未披露";
  }

  const direction = stockConnectMoneyDirection(amount);
  const unsigned = amount.startsWith("-") ? amount.slice(1) : amount;
  const formatted = /^[0-9]+(?:\.[0-9]+)?$/u.test(unsigned)
    ? formatStockConnectDecimal(unsigned)
    : unsigned;
  if (direction === "positive") {
    return `+ ${currency} ${formatted} · 净买入`;
  }
  if (direction === "negative") {
    return `− ${currency} ${formatted} · 净卖出`;
  }

  return `${currency} ${formatted} · 净额持平`;
}

/** 构造带固定原币的成交额 tooltip 格式化回调。 */
function createTurnoverTooltipFormatter(currency: string): (value: unknown) => string {
  /** 保留回调收到的真实数值，不进行资金流推导。 */
  return (value) => formatTrendTurnoverTooltip(value, currency);
}

/** 构造带固定原币的净额 tooltip 格式化回调。 */
function createNetTooltipFormatter(currency: string): (value: unknown) => string {
  /** 为每个真实净额补充中国市场方向文字。 */
  return (value) => formatTrendNetTooltip(value, currency);
}

/** 渲染已校验趋势画布和逐点可访问表；卸载时由图表 Hook 销毁引擎实例。 */
function StockConnectTrendCanvas({ channel, points, currency }: StockConnectTrendCanvasProps) {
  const tokens = useChartVisualTokens();
  const summaryId = `stock-connect-trend-summary-${channel.toLowerCase()}`;

  /** 构造不补点、不聚合且带中国市场净额语义的图表选项。 */
  const option = useMemo<EChartsOption>(
    () => ({
      animation: false,
      aria: {
        enabled: true,
        description: `${stockConnectChannelLabel(channel)}日终成交额与可用净额趋势，币种${currency}`,
      },
      backgroundColor: tokens.background,
      color: [tokens.series[0] ?? tokens.neutral, tokens.series[1] ?? tokens.neutral],
      dataset: {
        dimensions: ["tradeDate", "turnoverAmount", "netBuyAmount", "dataVersion"],
        source: points.map(toTrendRow),
      },
      grid: { top: 52, right: 20, bottom: 36, left: 72 },
      legend: {
        top: 12,
        textStyle: { color: tokens.textSecondary },
      },
      tooltip: { trigger: "axis" },
      visualMap: {
        type: "piecewise",
        show: false,
        seriesIndex: 1,
        dimension: 2,
        pieces: [
          { gt: 0, color: tokens.positive },
          { lt: 0, color: tokens.negative },
          { gte: 0, lte: 0, color: tokens.neutral },
        ],
      },
      xAxis: {
        type: "category",
        axisLabel: { color: tokens.textSecondary },
        axisLine: { lineStyle: { color: tokens.gridLine } },
      },
      yAxis: {
        type: "value",
        scale: true,
        name: currency,
        nameTextStyle: { color: tokens.textSecondary },
        axisLabel: { color: tokens.textSecondary },
        splitLine: {
          lineStyle: { color: tokens.gridLine, type: "dashed" },
        },
      },
      series: [
        {
          type: "line",
          name: `成交额（${currency}）`,
          encode: { x: "tradeDate", y: "turnoverAmount" },
          showSymbol: false,
          connectNulls: false,
          tooltip: { valueFormatter: createTurnoverTooltipFormatter(currency) },
        },
        {
          type: "line",
          name: `净额（${currency}，可用时）`,
          encode: { x: "tradeDate", y: "netBuyAmount" },
          showSymbol: false,
          connectNulls: false,
          tooltip: { valueFormatter: createNetTooltipFormatter(currency) },
        },
      ],
    }),
    [channel, currency, points, tokens],
  );
  const containerRef = useECharts(option);

  return (
    <Box sx={{ position: "relative" }}>
      <Box
        ref={containerRef}
        role="img"
        aria-label={`${stockConnectChannelLabel(channel)}日终成交额与可用净额趋势`}
        aria-describedby={summaryId}
        sx={{ width: "100%", minHeight: 320 }}
      />
      <Box
        component="table"
        id={summaryId}
        aria-label={`${stockConnectChannelLabel(channel)}趋势逐点数据`}
        sx={visuallyHiddenTrendTableSx}
      >
        <caption>{stockConnectChannelLabel(channel)}趋势逐点数据，金额保持来源原币</caption>
        <thead>
          <tr>
            <th scope="col">交易日</th>
            <th scope="col">通道成交额</th>
            <th scope="col">净额</th>
            <th scope="col">dataVersion</th>
          </tr>
        </thead>
        <tbody>
          {points.map(
            /** 逐点提供与图形完全相同的日期、金额和 publication 版本。 */
            (point) => (
              <tr key={`${point.tradeDate}-${point.dataVersion}`}>
                <td>{point.tradeDate}</td>
                <td>{formatStockConnectMoneyFact(point.stats.turnoverAmount)}</td>
                <td>{formatStockConnectNetFact(point.stats.netBuyAmount)}</td>
                <td>{point.dataVersion}</td>
              </tr>
            ),
          )}
        </tbody>
      </Box>
    </Box>
  );
}

/** 渲染单通道成交额与可用净额趋势，禁止跨币种合并或插值补点。 */
export function StockConnectTrendChart({ channel, points }: StockConnectTrendChartProps) {
  /** 从共同 bundle 趋势中隔离当前 URL 通道。 */
  const channelPoints = useMemo(
    () =>
      points.filter(
        /** 只保留 URL 选中通道，防止四通道趋势被错误叠加。 */
        (point) => point.channel === channel,
      ),
    [channel, points],
  );

  /** 收集当前通道真实金额事实的币种，禁止多币种进入同一坐标轴。 */
  const currencies = useMemo(() => {
    const result = new Set<string>();
    channelPoints.forEach(
      /** 收集同通道各金额事实的原币代码用于合同一致性检查。 */
      (point) => {
        const turnoverCurrency = point.stats.turnoverAmount.value?.currency;
        const netCurrency = point.stats.netBuyAmount.value?.currency;
        if (turnoverCurrency !== undefined) {
          result.add(turnoverCurrency);
        }
        if (netCurrency !== undefined) {
          result.add(netCurrency);
        }
      },
    );
    return [...result];
  }, [channelPoints]);
  const hasAmount = channelPoints.some(
    /** 确认至少存在一个可绘制的真实成交额或净额。 */
    (point) => point.stats.turnoverAmount.value !== null || point.stats.netBuyAmount.value !== null,
  );

  if (currencies.length > 1) {
    return <Alert severity="error">同一通道趋势出现多个币种，页面已停止绘制，避免错误合并。</Alert>;
  }

  if (!hasAmount) {
    return (
      <Stack
        role="status"
        spacing={1}
        justifyContent="center"
        alignItems="center"
        sx={{ minHeight: 300 }}
      >
        <Typography variant="subtitle1">该 publication 无可绘制金额</Typography>
        <Typography variant="body2" color="text.secondary">
          不使用示意折线，也不从成交额推导净买入。
        </Typography>
      </Stack>
    );
  }

  return (
    <StockConnectTrendCanvas
      channel={channel}
      points={channelPoints}
      currency={currencies[0] ?? "原币"}
    />
  );
}
