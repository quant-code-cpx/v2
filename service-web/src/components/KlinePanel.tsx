import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Box, CircularProgress, Stack } from "@mui/material";
import type { Chart, KLineData, Period } from "klinecharts";

import { useChartVisualTokens } from "../styles/chart-tokens";
import type { Candle } from "../types/candle";
import { createKlineDataLoader } from "../utils/kline-data-loader";

interface KlinePanelProps {
  symbol: string;
  period: Period;
  candles: readonly Candle[];
}

/** Convert validated domain candle into KLineChart's engine-specific data shape. */
function toKlineData(candle: Candle): KLineData {
  return {
    timestamp: candle.timestamp,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
    volume: candle.volume,
    turnover: candle.turnover,
  };
}

/** Lazily initialize KLineChart and keep its imperative engine synchronized with props. */
export function KlinePanel({ symbol, period, candles }: KlinePanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const barsRef = useRef<KLineData[]>([]);
  // Stable loader reads mutable ref, avoiding chart reinitialization when candle props update.
  const dataLoaderRef = useRef(createKlineDataLoader(() => barsRef.current));
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const chartTokens = useChartVisualTokens();

  barsRef.current = candles.map(toKlineData);

  /** Derive engine style object only when theme tokens change. */
  const styles = useMemo(
    () => ({
      grid: {
        horizontal: { color: chartTokens.gridLine },
        vertical: { color: chartTokens.gridLine },
      },
      candle: {
        bar: {
          upColor: chartTokens.positive,
          upBorderColor: chartTokens.positive,
          upWickColor: chartTokens.positive,
          downColor: chartTokens.negative,
          downBorderColor: chartTokens.negative,
          downWickColor: chartTokens.negative,
          noChangeColor: chartTokens.neutral,
          noChangeBorderColor: chartTokens.neutral,
          noChangeWickColor: chartTokens.neutral,
        },
      },
    }),
    [chartTokens],
  );

  // Lazy import keeps the heavy candlestick engine out of initial SPA bundle.
  useEffect(() => {
    let cancelled = false;
    let disposeChart: (() => void) | undefined;

    void import("klinecharts")
      .then(({ dispose, init }) => {
        if (cancelled || containerRef.current === null) {
          return;
        }

        const chart = init(containerRef.current, {
          locale: "zh-CN",
          timezone: "Asia/Shanghai",
          styles,
        });

        if (chart === null) {
          return;
        }

        chartRef.current = chart;
        chart.setSymbol({ ticker: symbol, pricePrecision: 2, volumePrecision: 0 });
        chart.setPeriod(period);
        chart.setDataLoader(dataLoaderRef.current);
        chart.createIndicator("MA", true);
        chart.createIndicator("VOL");
        // Dispose exact engine instance during React cleanup to prevent canvas and listener leaks.
        disposeChart = () => dispose(chart);
        setIsLoading(false);
      })
      .catch(() => {
        if (!cancelled) {
          setLoadError("K 线引擎加载失败，请刷新页面后重试。");
          setIsLoading(false);
        }
      });

    return () => {
      cancelled = true;
      chartRef.current = null;
      disposeChart?.();
    };
  }, []);

  // Symbols and periods share engine instance; reset data after switching either dimension.
  useEffect(() => {
    const chart = chartRef.current;

    if (chart === null) {
      return;
    }

    chart.setSymbol({ ticker: symbol, pricePrecision: 2, volumePrecision: 0 });
    chart.setPeriod(period);
    chart.resetData();
  }, [period, symbol]);

  /** Apply visual changes independently from chart data lifecycle. */
  useEffect(() => {
    chartRef.current?.setStyles(styles);
  }, [styles]);

  /** Reload chart bars after source props change while retaining configured indicators. */
  useEffect(() => {
    chartRef.current?.resetData();
  }, [candles]);

  return (
    <Box
      sx={{
        position: "relative",
        minHeight: 440,
        borderRadius: 2,
        overflow: "hidden",
        bgcolor: "background.paper",
      }}
    >
      {isLoading ? (
        <Stack
          alignItems="center"
          justifyContent="center"
          sx={{
            position: "absolute",
            inset: 0,
            zIndex: 2,
            pointerEvents: "none",
            color: "text.secondary",
          }}
        >
          <CircularProgress size={24} aria-label="正在载入 K 线图" />
        </Stack>
      ) : null}
      <Box ref={containerRef} sx={{ position: "relative", minHeight: 440, zIndex: 1 }} />
      {loadError === null ? null : (
        <Alert
          severity="error"
          sx={{ position: "absolute", inset: 12, zIndex: 3, alignSelf: "flex-start" }}
        >
          {loadError}
        </Alert>
      )}
      <Alert
        severity="info"
        variant="outlined"
        sx={{ position: "absolute", left: 12, bottom: 12, zIndex: 2, py: 0, pointerEvents: "none" }}
      >
        Fixture 数据 · 历史与实时接口接入后替换 DataLoader source
      </Alert>
    </Box>
  );
}
