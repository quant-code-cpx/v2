import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Box, CircularProgress, Stack, Typography } from "@mui/material";
import type { Chart, DataLoader, KLineData, Period } from "klinecharts";

import { useChartVisualTokens } from "../../../styles/chart-tokens";
import type { EquityBarPage } from "../../../types/equity-market";

/** 描述真实 publication K 线图输入。 */
interface EquityKlineChartProps {
  exchange: string;
  symbol: string;
  period: "1d" | "1w" | "1mo";
  page: EquityBarPage;
}

/** 将公开 decimal string 行情转换为 KLineChart 引擎数据。 */
function toKlineData(item: EquityBarPage["items"][number]): KLineData {
  return {
    timestamp: Date.parse(`${item.periodEnd}T00:00:00+08:00`),
    open: Number(item.open),
    high: Number(item.high),
    low: Number(item.low),
    close: Number(item.close),
    volume: Number(item.volumeShares),
    turnover: Number(item.amountCny),
  };
}

/** 将公开物理周期映射为 KLineChart 的 period 合同。 */
function toChartPeriod(period: EquityKlineChartProps["period"]): Period {
  if (period === "1w") return { span: 1, type: "week" };
  if (period === "1mo") return { span: 1, type: "month" };
  return { span: 1, type: "day" };
}

/** 渲染真实 API 行情的 KLineChart，并在 route/页签卸载时销毁精确实例。 */
export function EquityKlineChart({ exchange, symbol, period, page }: EquityKlineChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const barsRef = useRef<KLineData[]>([]);
  const [engineError, setEngineError] = useState(false);
  const [engineReady, setEngineReady] = useState(false);
  const tokens = useChartVisualTokens();

  barsRef.current = page.items.map(toKlineData);
  const dataLoaderRef = useRef<DataLoader>({
    /** 返回当前 immutable publication 窗口；实时订阅不在冻结合同内。 */
    getBars: ({ callback }) => {
      callback(barsRef.current, {
        forward: false,
        backward: false,
      });
    },
    /** 股票中心明确不注册未冻结的实时行情订阅。 */
    subscribeBar: () => undefined,
    /** 未注册订阅，因此清理操作保持无副作用。 */
    unsubscribeBar: () => undefined,
  });

  /** 从设计 Token 派生中国市场红涨绿跌的引擎样式。 */
  const styles = useMemo(
    () => ({
      grid: {
        horizontal: { color: tokens.gridLine },
        vertical: { color: tokens.gridLine },
      },
      candle: {
        bar: {
          upColor: tokens.positive,
          upBorderColor: tokens.positive,
          upWickColor: tokens.positive,
          downColor: tokens.negative,
          downBorderColor: tokens.negative,
          downWickColor: tokens.negative,
          noChangeColor: tokens.neutral,
          noChangeBorderColor: tokens.neutral,
          noChangeWickColor: tokens.neutral,
        },
      },
    }),
    [tokens],
  );

  // 引擎只在行情页签载入；清理同时释放 Canvas、观察器和 KLineChart 事件。
  useEffect(() => {
    let cancelled = false;
    let cleanup: (() => void) | undefined;

    void import("klinecharts")
      .then(({ dispose, init }) => {
        const element = containerRef.current;
        if (cancelled || element === null) return;

        const chart = init(element, {
          locale: "zh-CN",
          timezone: "Asia/Shanghai",
          styles,
        });
        if (chart === null) {
          setEngineError(true);
          return;
        }

        chartRef.current = chart;
        chart.setSymbol({ ticker: `${exchange}.${symbol}`, pricePrecision: 2, volumePrecision: 0 });
        chart.setPeriod(toChartPeriod(period));
        chart.setDataLoader(dataLoaderRef.current);
        chart.createIndicator("MA", true);
        chart.createIndicator("VOL");

        /** 容器尺寸变化时只通知命令式引擎，不写 React 高频状态。 */
        const resize = () => chart.resize();
        const observer = new ResizeObserver(resize);
        observer.observe(element);
        setEngineReady(true);
        cleanup = () => {
          observer.disconnect();
          dispose(chart);
        };
      })
      .catch(() => {
        if (!cancelled) setEngineError(true);
      });

    return () => {
      cancelled = true;
      chartRef.current = null;
      cleanup?.();
    };
  }, []);

  // 证券或上游物理周期变化时复用实例并从新 Query 实体重新加载。
  useEffect(() => {
    const chart = chartRef.current;
    if (chart === null) return;
    chart.setSymbol({ ticker: `${exchange}.${symbol}`, pricePrecision: 2, volumePrecision: 0 });
    chart.setPeriod(toChartPeriod(period));
    chart.resetData();
  }, [exchange, period, symbol]);

  // publication 窗口变化时保留指标与图表交互配置，只重置数据。
  useEffect(() => {
    chartRef.current?.resetData();
  }, [page]);

  // 视觉 Token 变化独立更新，不重建图表实例。
  useEffect(() => {
    chartRef.current?.setStyles(styles);
  }, [styles]);

  return (
    <Box sx={{ position: "relative", minHeight: 480, overflow: "hidden", borderRadius: 2 }}>
      {!engineReady && !engineError ? (
        <Stack
          role="status"
          direction="row"
          spacing={1}
          alignItems="center"
          justifyContent="center"
          sx={{ position: "absolute", inset: 0, zIndex: 2 }}
        >
          <CircularProgress size={24} />
          <Typography color="text.secondary">正在载入 KLineChart</Typography>
        </Stack>
      ) : null}
      <Box
        ref={containerRef}
        role="img"
        aria-label={`${exchange} ${symbol} ${period} K 线、均线与成交量`}
        sx={{ minHeight: 480, width: "100%" }}
      />
      {engineError ? (
        <Alert severity="error" sx={{ position: "absolute", inset: 2, zIndex: 3 }}>
          KLineChart 引擎加载失败，请重试当前页签。
        </Alert>
      ) : null}
    </Box>
  );
}
