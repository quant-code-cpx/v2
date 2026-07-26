import { useEffect, useRef } from "react";
import type { ECharts, EChartsOption } from "echarts";

import { echarts } from "../libs/echarts";

export function useECharts(option: EChartsOption) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<ECharts | null>(null);

  useEffect(() => {
    const element = containerRef.current;

    if (element === null) {
      return;
    }

    const chart = echarts.init(element);
    const resize = () => chart.resize();
    const observer = new ResizeObserver(resize);

    observer.observe(element);
    chartRef.current = chart;

    return () => {
      observer.disconnect();
      chartRef.current = null;
      chart.dispose();
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, { lazyUpdate: true, notMerge: true });
  }, [option]);

  return containerRef;
}
