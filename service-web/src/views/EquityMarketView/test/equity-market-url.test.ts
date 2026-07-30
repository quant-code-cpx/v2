import { describe, expect, it } from "vite-plus/test";

import {
  readEquityDetailUrl,
  readEquityMarketUrl,
  writeEquityDetailUrl,
  writeEquityMarketUrl,
} from "../utils/equity-market-url";

describe("equity market URL", () => {
  /** 列表筛选、publication cursor 和页号可以稳定复制与刷新。 */
  it("round-trips the complete list state", () => {
    const state = readEquityMarketUrl(
      new URLSearchParams(
        "q=600000&exchange=SSE&exchange=SZSE&status=LISTED&tradingStatus=TRADED" +
          "&tradingStatus=UNKNOWN" +
          "&industry=BK0475&concept=BK0816&sw=801780&sort=changePercent&order=desc" +
          "&page=2&limit=100&cursor=opaque-cursor" +
          "&dataVersion=8f401b48-5b0e-4a76-8d85-2c7101a28955",
      ),
    );

    expect(readEquityMarketUrl(writeEquityMarketUrl(state))).toEqual(state);
  });

  /** 非白名单枚举、未覆盖能力、非法 UUID 和超长分类代码不能进入 API 状态。 */
  it("normalizes unsafe and unsupported list values", () => {
    const state = readEquityMarketUrl(
      new URLSearchParams(
        `exchange=HKEX&status=SUSPENDED&sort=moneyFlowNetAmount&dataVersion=unsafe&industry=${"x".repeat(65)}`,
      ),
    );

    expect(state.exchanges).toEqual([]);
    expect(state.listingStatuses).toEqual(["LISTED"]);
    expect(state.sort).toBe("symbol");
    expect(state.dataVersion).toBeUndefined();
    expect(state.industries).toEqual([]);
  });

  /** 详情页签与 K 线控制可分享，高频十字线状态不会进入 URL。 */
  it("round-trips detail tab and chart controls", () => {
    const state = readEquityDetailUrl(
      new URLSearchParams("tab=events&period=1w&adjust=qfq&range=all&asOf=2009-12-29"),
    );

    expect(readEquityDetailUrl(writeEquityDetailUrl(state))).toEqual(state);
  });

  /** 未知详情参数回到行情、日线、未复权和三年窗口。 */
  it("falls back to the stable detail defaults", () => {
    expect(
      readEquityDetailUrl(new URLSearchParams("tab=unknown&period=5m&adjust=forward&range=10d")),
    ).toEqual({
      tab: "market",
      period: "1d",
      adjust: "none",
      range: "3y",
    });
  });
});
