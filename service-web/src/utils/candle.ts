import { candleDtoSchema, type Candle, type CandleDto } from "../types/candle";

/** Parse wire-format decimal and reject NaN or infinite chart values. */
function toFiniteNumber(value: string, field: string): number {
  const parsed = Number(value);

  if (!Number.isFinite(parsed)) {
    throw new Error(`Candle ${field} must be a finite number`);
  }

  return parsed;
}

/** Validate external candle DTO, enforce OHLC invariants, and return chart-ready numbers. */
export function normalizeCandle(input: CandleDto): Candle {
  const dto = candleDtoSchema.parse(input);
  const timestamp = Date.parse(dto.openTime);

  if (!Number.isFinite(timestamp)) {
    throw new Error("Candle openTime must be a valid timestamp");
  }

  const open = toFiniteNumber(dto.open, "open");
  const high = toFiniteNumber(dto.high, "high");
  const low = toFiniteNumber(dto.low, "low");
  const close = toFiniteNumber(dto.close, "close");
  const volume = toFiniteNumber(dto.volume, "volume");
  const turnover =
    dto.turnover === undefined ? undefined : toFiniteNumber(dto.turnover, "turnover");

  // Candlestick low/high must bound open and close; negative volume is never meaningful.
  if (low > Math.min(open, close) || high < Math.max(open, close) || low > high || volume < 0) {
    throw new Error("Candle violates OHLC or volume invariants");
  }

  return { timestamp, open, high, low, close, volume, turnover };
}
