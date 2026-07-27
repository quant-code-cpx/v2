import { describe, expect, it } from "vite-plus/test";

import type { CandleDto } from "../../types/candle";
import { normalizeCandle } from "../candle";

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

// 汇集图表边界转换与不变量拒绝测试。
describe("normalizeCandle", () => {
  // 验证线协议的小数值转换为图表引擎可用的数值。
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

  // 验证高低价顺序非法的数据不能进入蜡烛图渲染器。
  it("rejects invalid OHLC data", () => {
    expect(() => normalizeCandle({ ...validCandle, low: "1470.00" })).toThrow(
      "Candle violates OHLC or volume invariants",
    );
  });
});
