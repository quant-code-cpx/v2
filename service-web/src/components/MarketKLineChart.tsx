import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Box, CircularProgress, Stack } from "@mui/material";
import type { Chart, KLineData, Period } from "klinecharts";

import { useChartVisualTokens } from "../styles/chart-tokens";
import { createKlineDataLoader } from "../utils/kline-data-loader";

/** 描述已通过公开合同校验的一根来源 K 线。 */
export interface MarketChartBar {
  date: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume?: string;
  amount?: string;
}

/** 描述 KLineChart 的稳定证券或板块身份与原生周期。 */
interface MarketKLineChartProps {
  identity: string;
  period: "1d" | "1w" | "1mo";
  bars: readonly MarketChartBar[];
  height?: number;
}

/** 将公开周期枚举映射为 KLineChart 引擎周期，不在浏览器聚合周期。 */
function chartPeriod(period: MarketKLineChartProps["period"]): Period {
  if (period === "1w") {
    return { span: 1, type: "week" };
  }
  if (period === "1mo") {
    return { span: 1, type: "month" };
  }
  return { span: 1, type: "day" };
}

/** 将合同十进制字段转换为图表显示值；转换结果不参与资金或排名计算。 */
function toKlineData(bar: MarketChartBar): KLineData {
  return {
    timestamp: new Date(`${bar.date}T00:00:00+08:00`).getTime(),
    open: Number(bar.open),
    high: Number(bar.high),
    low: Number(bar.low),
    close: Number(bar.close),
    ...(bar.volume === undefined ? {} : { volume: Number(bar.volume) }),
    turnover: bar.amount === undefined ? undefined : Number(bar.amount),
  };
}

/** 懒初始化 KLineChart，并在 identity、周期、数据或主题变化时复用同一引擎实例。 */
export function MarketKLineChart({ identity, period, bars, height = 420 }: MarketKLineChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const barsRef = useRef<KLineData[]>([]);
  const dataLoaderRef = useRef(
    createKlineDataLoader(
      /** 让稳定 DataLoader 每次读取最新不可变 bars 投影。 */
      () => barsRef.current,
    ),
  );
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const chartTokens = useChartVisualTokens();

  barsRef.current = bars.map(toKlineData);

  /** 仅在 canonical 图表 Token 变化时重建引擎样式。 */
  const styles = useMemo(
    /** 把浅色主题与中国市场方向色投影为 KLineChart 样式。 */
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

  // 动态 import 保证 KLineChart 不进入应用基础 Bundle。
  useEffect(() => {
    let cancelled = false;
    let disposeChart: (() => void) | undefined;

    void import("klinecharts")
      .then(
        /** 初始化命令式引擎并绑定稳定 DataLoader。 */
        ({ dispose, init }) => {
          if (cancelled || containerRef.current === null) {
            return;
          }
          const chart = init(containerRef.current, {
            locale: "zh-CN",
            timezone: "Asia/Shanghai",
            styles,
          });
          if (chart === null) {
            setLoadError("K 线引擎初始化失败，请局部重试。");
            setIsLoading(false);
            return;
          }
          chartRef.current = chart;
          chart.setSymbol({ ticker: identity, pricePrecision: 3, volumePrecision: 0 });
          chart.setPeriod(chartPeriod(period));
          chart.setDataLoader(dataLoaderRef.current);
          chart.createIndicator("MA", true);
          chart.createIndicator("VOL");
          /** 记录当前精确实例的销毁动作，路由卸载时释放 Canvas。 */
          disposeChart = () => dispose(chart);
          setIsLoading(false);
        },
      )
      .catch(
        /** 将引擎 chunk 失败收敛为本图表错误，不卸载页面其余真实数据。 */
        () => {
          if (!cancelled) {
            setLoadError("K 线引擎加载失败，请刷新页面后重试。");
            setIsLoading(false);
          }
        },
      );

    /** 销毁精确 Canvas 与监听器，防止路由切换泄漏。 */
    return () => {
      cancelled = true;
      chartRef.current = null;
      disposeChart?.();
    };
  }, []);

  // identity 或后端原生周期变化时重置同一引擎的数据窗口。
  useEffect(() => {
    const chart = chartRef.current;
    if (chart === null) {
      return;
    }
    chart.setSymbol({ ticker: identity, pricePrecision: 3, volumePrecision: 0 });
    chart.setPeriod(chartPeriod(period));
    chart.resetData();
  }, [identity, period]);

  // 新 publication 或页数据替换后重新加载，不重建图表实例。
  useEffect(() => {
    chartRef.current?.resetData();
  }, [bars]);

  // 主题 Token 变化只更新视觉参数。
  useEffect(() => {
    chartRef.current?.setStyles(styles);
  }, [styles]);

  return (
    <Box
      role="img"
      aria-label={`${identity} ${period} K 线图`}
      sx={{
        position: "relative",
        minHeight: height,
        borderRadius: 2,
        overflow: "hidden",
        bgcolor: "background.paper",
      }}
    >
      {isLoading ? (
        <Stack
          alignItems="center"
          justifyContent="center"
          sx={{ position: "absolute", inset: 0, zIndex: 2, color: "text.secondary" }}
        >
          <CircularProgress size={24} aria-label="正在载入 K 线图" />
        </Stack>
      ) : null}
      <Box ref={containerRef} sx={{ position: "relative", minHeight: height, zIndex: 1 }} />
      {loadError === null ? null : (
        <Alert severity="error" sx={{ position: "absolute", inset: 2, zIndex: 3 }}>
          {loadError}
        </Alert>
      )}
    </Box>
  );
}
