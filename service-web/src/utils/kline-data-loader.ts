import type { DataLoader, KLineData } from "klinecharts";

export type KlineBarsSource = () => readonly KLineData[];

export function normalizeKlineBars(bars: readonly KLineData[]): KLineData[] {
  const latestByTimestamp = new Map<number, KLineData>();

  for (const bar of bars) {
    latestByTimestamp.set(bar.timestamp, bar);
  }

  return Array.from(latestByTimestamp.values()).toSorted(
    (left, right) => left.timestamp - right.timestamp,
  );
}

export function createKlineDataLoader(source: KlineBarsSource): DataLoader {
  return {
    getBars: ({ callback }) => {
      callback(normalizeKlineBars(source()), { forward: false, backward: false });
    },
    subscribeBar: () => undefined,
    unsubscribeBar: () => undefined,
  };
}
