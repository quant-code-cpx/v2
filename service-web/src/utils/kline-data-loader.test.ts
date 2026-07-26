import type { KLineData } from "klinecharts";
import { describe, expect, it, vi } from "vite-plus/test";

import { createKlineDataLoader, normalizeKlineBars } from "./kline-data-loader";

const bars: KLineData[] = [
  { timestamp: 2, open: 2, high: 3, low: 1, close: 2.5 },
  { timestamp: 1, open: 1, high: 2, low: 0.5, close: 1.5 },
  { timestamp: 2, open: 2, high: 4, low: 1, close: 3 },
];

// Group KLineChart adapter callbacks around ordering and bounded-history behavior.
describe("createKlineDataLoader", () => {
  // Verify source updates collapse by timestamp before chart sees bars.
  it("sorts bars and keeps latest update for duplicate timestamps", () => {
    expect(normalizeKlineBars(bars)).toEqual([
      { timestamp: 1, open: 1, high: 2, low: 0.5, close: 1.5 },
      { timestamp: 2, open: 2, high: 4, low: 1, close: 3 },
    ]);
  });

  // Verify adapter invokes engine callback with no forward or backward fixture pages.
  it("returns a bounded fixture page through KLineChart callback", () => {
    const callback = vi.fn();
    const loader = createKlineDataLoader(() => bars);

    void loader.getBars({
      type: "init",
      timestamp: null,
      symbol: { ticker: "600519", pricePrecision: 2, volumePrecision: 0 },
      period: { type: "day", span: 1 },
      callback,
    });

    expect(callback).toHaveBeenCalledWith(
      [
        { timestamp: 1, open: 1, high: 2, low: 0.5, close: 1.5 },
        { timestamp: 2, open: 2, high: 4, low: 1, close: 3 },
      ],
      { forward: false, backward: false },
    );
  });
});
