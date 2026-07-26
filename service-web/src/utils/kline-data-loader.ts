import type { DataLoader, KLineData } from "klinecharts";

export type KlineBarsSource = () => readonly KLineData[];

/** Deduplicate bars by latest timestamp update and return chronological chart order. */
export function normalizeKlineBars(bars: readonly KLineData[]): KLineData[] {
  const latestByTimestamp = new Map<number, KLineData>();

  for (const bar of bars) {
    latestByTimestamp.set(bar.timestamp, bar);
  }

  // Later source entries overwrite same timestamp, matching incremental-bar update semantics.
  return Array.from(latestByTimestamp.values()).toSorted(
    (left, right) => left.timestamp - right.timestamp,
  );
}

/** Adapt current in-memory bars into bounded KLineChart data-loader contract. */
export function createKlineDataLoader(source: KlineBarsSource): DataLoader {
  return {
    /** Return all fixture bars and declare no additional history in either direction. */
    getBars: ({ callback }) => {
      callback(normalizeKlineBars(source()), { forward: false, backward: false });
    },
    /** No-op until real-time candle subscription contract exists. */
    subscribeBar: () => undefined,
    /** No-op companion for absent real-time candle subscription. */
    unsubscribeBar: () => undefined,
  };
}
