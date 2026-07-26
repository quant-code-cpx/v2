import { useEffect, useRef } from "react";
import type { ECharts, EChartsOption } from "echarts";

import { echarts } from "../libs/echarts";

/** Bind one ECharts canvas to a React element and apply latest immutable option. */
export function useECharts(option: EChartsOption) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<ECharts | null>(null);

  /** Create a container-resizing chart engine once and dispose it with component. */
  useEffect(() => {
    const element = containerRef.current;

    if (element === null) {
      return;
    }

    const chart = echarts.init(element);
    /** Resize engine whenever container dimensions change. */
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

  /** Replace chart option without recreating engine. */
  useEffect(() => {
    chartRef.current?.setOption(option, { lazyUpdate: true, notMerge: true });
  }, [option]);

  return containerRef;
}
