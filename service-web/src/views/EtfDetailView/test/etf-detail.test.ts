import { describe, expect, it } from "vite-plus/test";

import {
  createEtfNavPricePoints,
  latestEtfStates,
  parseEtfRouteIdentity,
} from "../utils/etf-detail";
import { hasPartialDatasetFailure } from "../EtfDetailView";
import type { EtfDailyBarValues, EtfNavValues, EtfTradingStateValues } from "../../../types/etf";

/** 返回详情纯函数测试使用的最小未复权日线。 */
function bar(tradeDate: string, close: string): EtfDailyBarValues {
  return {
    tradeDate,
    etfEntityRef: "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91",
    open: close,
    high: close,
    low: close,
    close,
    volume: "100",
    volumeUnit: "LOT",
    amount: "1000",
    currency: "CNY",
    tradeStatus: null,
    adjustment: "UNADJUSTED",
  };
}

/** 返回详情纯函数测试使用的最小单位 NAV。 */
function nav(navDate: string, value: string): EtfNavValues {
  return {
    navDate,
    etfEntityRef: "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91",
    navKind: "UNIT",
    nav: value,
    currency: "CNY",
    finality: "FINAL",
  };
}

/** 返回指定维度和日期的来源状态。 */
function state(
  stateDimension: EtfTradingStateValues["stateDimension"],
  effectiveFrom: string,
  value: string,
): EtfTradingStateValues {
  return {
    etfEntityRef: "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91",
    stateDimension,
    state: value,
    effectiveFrom,
    effectiveTo: null,
    reason: null,
  };
}

describe("ETF detail derivation", () => {
  /** 路由只校验显式交易所与代码格式，不根据代码前缀补交易所。 */
  it("validates explicit route identity without venue inference", () => {
    expect(parseEtfRouteIdentity("SSE", "510300")).toEqual({
      exchange: "SSE",
      symbol: "510300",
    });
    expect(parseEtfRouteIdentity(undefined, "510300")).toBeNull();
    expect(parseEtfRouteIdentity("UNKNOWN", "510300")).toBeNull();
  });

  /** 价格与 NAV 仅按日期并集对齐原值，不产出折溢价字段或插值。 */
  it("aligns raw price and NAV values without premium calculation", () => {
    const points = createEtfNavPricePoints(
      [bar("2026-07-28", "3.90"), bar("2026-07-29", "3.98")],
      [nav("2026-07-29", "3.97"), nav("2026-07-30", "4.01")],
    );

    expect(points).toEqual([
      { date: "2026-07-28", close: 3.9, nav: null },
      { date: "2026-07-29", close: 3.98, nav: 3.97 },
      { date: "2026-07-30", close: null, nav: 4.01 },
    ]);
    expect(points.every((point) => !("premium" in point))).toBe(true);
  });

  /** 各状态维度只保留自身最后生效值，交易状态缺失时不会由申购状态补齐。 */
  it("keeps ETF state dimensions independent", () => {
    const latest = latestEtfStates([
      state("SUBSCRIPTION", "2026-07-29", "CLOSED"),
      state("SUBSCRIPTION", "2026-07-28", "OPEN"),
      state("REDEMPTION", "2026-07-29", "OPEN"),
    ]);

    expect(latest.get("SUBSCRIPTION")?.state).toBe("CLOSED");
    expect(latest.get("REDEMPTION")?.state).toBe("OPEN");
    expect(latest.get("TRADING")).toBeUndefined();
  });

  /** 已缓存 publication 的刷新错误仍须进入部分失败，不能被 available 展示状态掩盖。 */
  it("treats a failed refresh with cached data as a partial failure", () => {
    expect(
      hasPartialDatasetFailure([
        { state: "available", isError: true },
        { state: "available", isError: false },
      ]),
    ).toBe(true);
    expect(
      hasPartialDatasetFailure([
        { state: "available", isError: false },
        { state: "empty", isError: false },
      ]),
    ).toBe(false);
  });
});
