import { describe, expect, it } from "vite-plus/test";

import type { CandleDto } from "../types/candle";
import { normalizeCandle } from "./candle";

const validCandle: CandleDto = {
  symbol: "600519",
  interval: "1d",
  openTime: "2026-07-24T01:30:00.000Z",
  open: "1460.00",
  high: "1472.00",
  low: "1458.00",
  close: "1468.22",
  volume: "1280000",
  turnover: "1879321600",
  sequence: "42",
  isClosed: true,
};

// Group chart-boundary conversion and invariant-rejection callbacks.
describe("normalizeCandle", () => {
  // Verify decimal wire values become numbers suitable for chart engines.
  it("converts decimal strings at chart boundary", () => {
    expect(normalizeCandle(validCandle)).toMatchObject({
      open: 1460,
      high: 1472,
      low: 1458,
      close: 1468.22,
      volume: 1_280_000,
      turnover: 1_879_321_600,
    });
  });

  // Verify invalid low/high ordering cannot enter candlestick renderer.
  it("rejects invalid OHLC data", () => {
    expect(() => normalizeCandle({ ...validCandle, low: "1470.00" })).toThrow(
      "Candle violates OHLC or volume invariants",
    );
  });
});
