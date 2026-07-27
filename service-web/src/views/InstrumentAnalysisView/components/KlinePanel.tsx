import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Box, CircularProgress, Stack } from "@mui/material";
import type { Chart, KLineData, Period } from "klinecharts";

import { useChartVisualTokens } from "../../../styles/chart-tokens";
import type { Candle } from "../../../types/candle";
import { createKlineDataLoader } from "../../../utils/kline-data-loader";

interface KlinePanelProps {
  symbol: string;
  period: Period;
  candles: readonly Candle[];
}

/** 将已验证领域 Candle 转换为 KLineChart 引擎数据结构。 */
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

/** 懒初始化 KLineChart，并让命令式引擎与属性保持同步。 */
export function KlinePanel({ symbol, period, candles }: KlinePanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const barsRef = useRef<KLineData[]>([]);
  // 稳定 DataLoader 读取可变 ref，避免 Candle 属性变化时重建图表。
  const dataLoaderRef = useRef(createKlineDataLoader(() => barsRef.current));
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const chartTokens = useChartVisualTokens();

  barsRef.current = candles.map(toKlineData);

  /** 仅在主题 Token 变化时派生引擎样式对象。 */
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

  // 懒加载避免 K 线引擎进入 SPA 初始 Bundle。
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
        // React 清理阶段销毁精确引擎实例，避免 Canvas 与监听器泄漏。
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

  // 标的与周期共享引擎实例，任一维度变化后重置数据。
  useEffect(() => {
    const chart = chartRef.current;

    if (chart === null) {
      return;
    }

    chart.setSymbol({ ticker: symbol, pricePrecision: 2, volumePrecision: 0 });
    chart.setPeriod(period);
    chart.resetData();
  }, [period, symbol]);

  /** 独立于图表数据生命周期应用视觉变化。 */
  useEffect(() => {
    chartRef.current?.setStyles(styles);
  }, [styles]);

  /** 来源属性变化后重新加载 K 线，同时保留已配置指标。 */
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
