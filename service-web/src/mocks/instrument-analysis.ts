import type { Candle, CandleDto } from "../types/candle";
import { normalizeCandle } from "../utils/candle";

function createDemoCandleDtos(): CandleDto[] {
  const firstOpenTime = Date.UTC(2026, 0, 2);
  let previousClose = 1_420;

  return Array.from({ length: 160 }, (_, index) => {
    const trend = index * 0.42;
    const wave = Math.sin(index / 7) * 18 + Math.cos(index / 17) * 7;
    const open = previousClose;
    const close = Math.max(1, open + trend / 10 + wave / 4);
    const high = Math.max(open, close) + 4 + (index % 5);
    const low = Math.min(open, close) - 4 - (index % 3);
    const volume = 1_000_000 + ((index * 71_137) % 740_000);
    const openTime = new Date(firstOpenTime + index * 86_400_000).toISOString();

    previousClose = close;

    return {
      symbol: "600519",
      interval: "1d",
      openTime,
      open: open.toFixed(2),
      high: high.toFixed(2),
      low: low.toFixed(2),
      close: close.toFixed(2),
      volume: volume.toFixed(0),
      turnover: (close * volume).toFixed(2),
      sequence: String(index + 1),
      isClosed: index < 159,
    };
  });
}

export const demoCandles: readonly Candle[] = createDemoCandleDtos().map(normalizeCandle);

export const demoAnalysisSeries = demoCandles.map((candle, index) => ({
  date: new Date(candle.timestamp).toLocaleDateString("zh-CN", {
    month: "numeric",
    day: "numeric",
  }),
  close: candle.close,
  benchmark: 1_420 + index * 0.35 + Math.sin(index / 11) * 9,
}));
