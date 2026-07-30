import { describe, expect, it } from "vite-plus/test";

import {
  readEtfListUrlState,
  resetEtfListCursor,
  writeEtfListUrlState,
} from "../utils/etf-list-url";

describe("ETF list URL state", () => {
  /** URL 只接受冻结交易所、真实关键词、排序和有界 cursor/page。 */
  it("reads only allowlisted ETF filters and cursor state", () => {
    const filters = readEtfListUrlState(
      new URLSearchParams(
        "exchange=SZSE&q=%E6%B2%AA%E6%B7%B1&listingStatus=LISTED&status=LISTED&sort=displayName&order=desc&cursor=opaque-1&page=2&provider=forbidden",
      ),
    );

    expect(filters).toEqual({
      exchange: "SZSE",
      q: "沪深",
      sort: "displayName",
      order: "desc",
      cursor: "opaque-1",
      page: 2,
      pageSize: 50,
    });
    expect(filters).not.toHaveProperty("listingStatus");
    expect(writeEtfListUrlState(filters).toString()).not.toContain("provider");
    expect(writeEtfListUrlState(filters).toString()).not.toContain("status");
    expect(writeEtfListUrlState(filters).toString()).not.toContain("listingStatus");
  });

  /** 未知枚举、非法页码和过长 cursor 必须安全回到目录首页。 */
  it("rejects unsupported values without guessing alternatives", () => {
    const filters = readEtfListUrlState(
      new URLSearchParams(`exchange=UNKNOWN&status=REMOVED&page=-1&cursor=${"x".repeat(2_049)}`),
    );

    expect(filters).toMatchObject({
      exchange: "SSE",
      sort: "symbol",
      order: "asc",
      page: 1,
      pageSize: 50,
    });
    expect(filters).not.toHaveProperty("listingStatus");
    expect(filters.cursor).toBeUndefined();
  });

  /** URL 缺少服务端 cursor 时页码不能脱离真实数据页独立增长。 */
  it("forces the first page when a shared URL has no opaque cursor", () => {
    const filters = readEtfListUrlState(new URLSearchParams("page=99"));

    expect(filters.page).toBe(1);
    expect(filters.cursor).toBeUndefined();
  });

  /** URL 保留最长四十位通用关键词，具体字段语义由 typed 查询适配器安全决定。 */
  it("preserves a forty-character numeric keyword without treating it as a symbol", () => {
    const query = "1".repeat(40);
    const filters = readEtfListUrlState(new URLSearchParams({ q: query }));

    expect(filters.q).toBe(query);
    expect(writeEtfListUrlState(filters).get("q")).toBe(query);
  });

  /** 改变筛选或排序时必须清除不再属于请求指纹的 opaque cursor。 */
  it("clears cursor and page when filters change", () => {
    const next = resetEtfListCursor(
      {
        exchange: "SSE",
        sort: "symbol",
        order: "asc",
        cursor: "opaque-2",
        page: 3,
        pageSize: 50,
      },
      { sort: "displayName", order: "desc" },
    );

    expect(next).toMatchObject({ sort: "displayName", order: "desc", page: 1 });
    expect(next.cursor).toBeUndefined();
  });
});
