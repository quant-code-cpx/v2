import { describe, expect, it } from "vite-plus/test";

import {
  isExactTradeDate,
  parseStockConnectSecurityUrlState,
  parseStockConnectUrlState,
  serializeStockConnectChannelDetailUrlState,
  serializeStockConnectSecurityUrlState,
  serializeStockConnectUrlState,
  toStockConnectDateSelection,
} from "../utils/stock-connect-url";

/** 验证互联互通可分享 URL 的规范化与各路由参数所有权。 */
describe("stock connect URL state", () => {
  /** 非法日期、枚举、数值和过长游标必须回到冻结默认值。 */
  it("normalizes unsupported URL values without guessing", () => {
    const parameters = new URLSearchParams({
      date: "2026-02-30",
      direction: "global",
      channel: "unknown",
      ranking: "turnover-as-flow",
      trendDays: "30",
      pageSize: "999",
      cursor: "x".repeat(1025),
    });

    expect(parseStockConnectUrlState(parameters)).toEqual({
      date: "latest",
      direction: "all",
      channel: "sh-northbound",
      ranking: "active",
      trendDays: 20,
      pageSize: 20,
    });
  });

  /** 方向与趋势通道冲突时应选择该方向首条通道，避免无效组合。 */
  it("keeps the selected channel inside the requested direction", () => {
    const parameters = new URLSearchParams({
      direction: "southbound",
      channel: "sh-northbound",
    });

    expect(parseStockConnectUrlState(parameters)).toMatchObject({
      direction: "southbound",
      channel: "sh-southbound",
    });
  });

  /** 精确日、榜内排序和不透明游标应完整序列化并可复现。 */
  it("round trips shareable exact-date filters and cursor", () => {
    const state = {
      date: "2026-07-30" as const,
      direction: "northbound" as const,
      channel: "sz-northbound" as const,
      ranking: "net-sell" as const,
      trendDays: 120 as const,
      pageSize: 50 as const,
      cursor: "opaque-cursor",
    };
    const serialized = serializeStockConnectUrlState(state);

    expect(parseStockConnectUrlState(serialized)).toEqual(state);
    expect(toStockConnectDateSelection(state.date)).toEqual({
      mode: "EXACT",
      exactDate: "2026-07-30",
    });
  });

  /** 单通道详情由 path 唯一持有通道，不得保留冲突方向或重复 channel 查询参数。 */
  it("keeps the channel path as the sole channel source on detail pages", () => {
    const serialized = serializeStockConnectChannelDetailUrlState({
      date: "2026-07-30",
      direction: "southbound",
      channel: "sz-southbound",
      ranking: "net-buy",
      trendDays: 60,
      pageSize: 50,
      cursor: "opaque-cursor",
    });

    expect(serialized.toString()).toBe(
      "date=2026-07-30&ranking=net-buy&trendDays=60&pageSize=50&cursor=opaque-cursor",
    );
  });

  /** 公历日期校验应接受闰日并拒绝不存在日期。 */
  it("validates exact Gregorian dates", () => {
    expect(isExactTradeDate("2028-02-29")).toBe(true);
    expect(isExactTradeDate("2027-02-29")).toBe(false);
    expect(isExactTradeDate("latest")).toBe(false);
  });

  /** 证券上下文缺省通道必须映射 all/null 语义并保持精确日。 */
  it("keeps security channel optional without inventing an aggregate channel", () => {
    const state = parseStockConnectSecurityUrlState(
      new URLSearchParams({ date: "2026-07-30", trendDays: "60" }),
    );

    expect(state).toEqual({
      date: "2026-07-30",
      trendDays: 60,
    });
    expect(serializeStockConnectSecurityUrlState(state).toString()).toBe(
      "date=2026-07-30&trendDays=60",
    );
  });
});
