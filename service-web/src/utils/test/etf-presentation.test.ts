import { describe, expect, it } from "vite-plus/test";

import {
  etfAvailabilityState,
  publicationDateMismatch,
  publicationShanghaiDate,
  unavailableReleaseSummary,
} from "../etf-presentation";
import type { MarketDataPageMeta } from "../../types/etf";

/** 构造仅供 publication 日期测试使用的最小可用元数据。 */
function availableMeta(publishedAt: string): MarketDataPageMeta {
  return {
    requestId: "request-1",
    contractVersion: "1.0.0",
    dataset: { code: "fund.etf.profile.reported", schemaVersion: 2 },
    availability: "AVAILABLE",
    release: {
      dataVersion: "00000000-0000-4000-8000-000000000001",
      publishedAt,
      knowledgeCutoff: publishedAt,
      publicUsableAt: publishedAt,
      effectiveFrom: null,
      effectiveTo: null,
      methodology: {},
      sources: [],
      quality: {},
      completeness: "UNKNOWN",
    },
    visibility: {},
    page: { limit: 50, hasMore: false, nextCursor: null },
    coverage: {},
    warnings: [],
    disclaimers: [],
  };
}

describe("ETF 展示语义", () => {
  /** 验证合法空结果与明确不支持都不会被映射为来源故障。 */
  it("区分 AVAILABLE、EMPTY、SOURCE_UNAVAILABLE 与 CURRENTLY_UNSUPPORTED", () => {
    expect(etfAvailabilityState("AVAILABLE")).toBe("available");
    expect(etfAvailabilityState("EMPTY")).toBe("empty");
    expect(etfAvailabilityState("SOURCE_UNAVAILABLE")).toBe("source-unavailable");
    expect(etfAvailabilityState("CURRENTLY_UNSUPPORTED")).toBe("currently-unsupported");
  });

  /** 验证 UTC 日界附近的 publication 仍按上海日历日展示。 */
  it("按 Asia/Shanghai 转换 publication 日期", () => {
    expect(publicationShanghaiDate("2026-07-29T16:30:00.000Z")).toBe("2026-07-30");
  });

  /** 验证同一上海日历日不会被 UTC 日期差异误报为独立发布。 */
  it("仅在上海 publication 日历日不一致时提示", () => {
    expect(
      publicationDateMismatch([
        { label: "产品资料", meta: availableMeta("2026-07-29T15:30:00.000Z") },
        { label: "日线", meta: availableMeta("2026-07-29T16:30:00.000Z") },
      ]),
    ).toBe("各数据集独立发布：产品资料 2026-07-29；日线 2026-07-30");
    expect(
      publicationDateMismatch([
        { label: "产品资料", meta: availableMeta("2026-07-29T16:30:00.000Z") },
        { label: "日线", meta: availableMeta("2026-07-30T01:00:00.000Z") },
      ]),
    ).toBeNull();
  });

  /** 无 publication 状态必须保留原因、观测时间和同步警告。 */
  it("展示不可用 publication 的完整可观测证据", () => {
    const summary = unavailableReleaseSummary({
      requestId: "market-data-request-72",
      contractVersion: "1.0.0",
      dataset: { code: "fund.etf.profile.reported", schemaVersion: 2 },
      availability: "SOURCE_UNAVAILABLE",
      release: {
        state: "SOURCE_UNAVAILABLE",
        observedAt: "2026-07-30T01:00:00.000Z",
        reasonCode: "PROVIDER_UNAVAILABLE",
      },
      visibility: { mode: "CURRENT" },
      page: { limit: 50, hasMore: false, nextCursor: null },
      coverage: {},
      warnings: ["最近一次成功 publication 已延迟"],
      disclaimers: [],
    });

    expect(summary).toContain("PROVIDER_UNAVAILABLE；最近观测");
    expect(summary).toContain("最近一次成功 publication 已延迟");
  });
});
