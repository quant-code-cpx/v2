import { z } from "zod";

const decimalString = z.string().regex(/^-?\d+(?:\.\d+)?$/);

export const candleDtoSchema = z.object({
  symbol: z.string().min(1),
  interval: z.string().min(1),
  openTime: z.iso.datetime(),
  open: decimalString,
  high: decimalString,
  low: decimalString,
  close: decimalString,
  volume: decimalString,
  turnover: decimalString.optional(),
  sequence: z.string().min(1),
  isClosed: z.boolean(),
});

export type CandleDto = z.infer<typeof candleDtoSchema>;

export interface Candle {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  turnover?: number;
}
