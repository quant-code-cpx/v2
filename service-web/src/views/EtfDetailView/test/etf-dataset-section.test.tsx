import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { EtfDatasetSection } from "../components/EtfDatasetSection";
import type { MarketDataPageMeta } from "../../../types/etf";

/** 构造已通过公开合同校验的 ETF 日线 publication 元数据。 */
function availableMeta(): MarketDataPageMeta {
  return {
    requestId: "etf-detail-refresh-1",
    contractVersion: "1.0.0",
    dataset: { code: "fund.etf.bar.1d.reported", schemaVersion: 2 },
    availability: "AVAILABLE",
    release: {
      dataVersion: "00000000-0000-4000-8000-000000000201",
      publishedAt: "2026-07-30T09:00:00Z",
      knowledgeCutoff: "2026-07-30T08:59:00Z",
      publicUsableAt: "2026-07-30T09:00:00Z",
      effectiveFrom: null,
      effectiveTo: null,
      methodology: { code: "etf-unadjusted-daily-bar", version: "1", kind: "REPORTED" },
      sources: [],
      quality: { status: "PASSED", issueCodes: [] },
      completeness: "COMPLETE",
    },
    visibility: { mode: "CURRENT" },
    page: { limit: 366, hasMore: false, nextCursor: null },
    coverage: { from: "2025-07-30", to: "2026-07-30" },
    warnings: [],
    disclaimers: [],
  };
}

/** 构造货币市场 ETF NAV 口径当前不支持的成功空状态。 */
function unsupportedNavMeta(): MarketDataPageMeta {
  return {
    requestId: "etf-detail-unsupported-nav-1",
    contractVersion: "1.0.0",
    dataset: { code: "fund.etf.nav.1d.reported", schemaVersion: 2 },
    availability: "CURRENTLY_UNSUPPORTED",
    release: {
      state: "CURRENTLY_UNSUPPORTED",
      observedAt: "2026-07-30T09:00:00Z",
      reasonCode: "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
    },
    visibility: { mode: "CURRENT" },
    page: { limit: 366, hasMore: false, nextCursor: null },
    coverage: { pitCoverage: "UNKNOWN" },
    warnings: [],
    disclaimers: [],
  };
}

/** 覆盖详情区块在后台刷新失败时保留已校验 publication 的降级展示。 */
describe("EtfDatasetSection", () => {
  /** 每个用例后卸载区块，避免前一状态的重试按钮污染后续断言。 */
  afterEach(() => {
    cleanup();
  });

  /** 刷新失败必须在区块内明示，同时保留 publication、业务内容与独立重试。 */
  it("keeps cached content visible and shows a refresh warning", () => {
    const onRetry = vi.fn();

    render(
      <EtfDatasetSection
        title="日线行情"
        description="来源未复权日线"
        datasetLabel="ETF 未复权日线 v2"
        state="available"
        meta={availableMeta()}
        refreshFailed
        onRetry={onRetry}
      >
        <div>已缓存 K 线</div>
      </EtfDatasetSection>,
    );

    expect(
      screen.getByText("日线行情刷新失败，仍展示上一份已校验 publication。"),
    ).toBeInTheDocument();
    expect(screen.getByText("已缓存 K 线")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  /** 货币市场收益口径不能安全映射为 NAV 时显示明确不支持且不渲染业务内容。 */
  it("shows a distinct currently-unsupported NAV state without fabricated values", () => {
    render(
      <EtfDatasetSection
        title="价格与单位 NAV"
        description="来源原值"
        datasetLabel="ETF 单位 NAV v2"
        state="currently-unsupported"
        meta={unsupportedNavMeta()}
        onRetry={vi.fn()}
      >
        <div>不应展示的 NAV</div>
      </EtfDatasetSection>,
    );

    expect(screen.getByText("价格与单位 NAV当前不支持")).toBeInTheDocument();
    expect(screen.getByText(/NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET/u)).toBeInTheDocument();
    expect(screen.queryByText("不应展示的 NAV")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });
});
