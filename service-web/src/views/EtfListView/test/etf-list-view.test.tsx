import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vite-plus/test";

import type { EtfProfileValues, MarketDataPage } from "../../../types/etf";
import { EtfListView } from "../EtfListView";

const { useEtfListMock } = vi.hoisted(() => ({
  useEtfListMock: vi.fn(),
}));

vi.mock("../hooks/useEtfList", () => ({
  /** 让目录视图测试注入已发布但筛选结果为空的 typed page。 */
  useEtfList: useEtfListMock,
}));

/** 构造 AVAILABLE publication 下的合法筛选空页。 */
function availableEmptyPage(): MarketDataPage<EtfProfileValues> {
  return {
    meta: {
      requestId: "72a4d2a1-3798-4bcf-978f-75c69c6d246b",
      contractVersion: "1.0.0",
      dataset: { code: "fund.etf.profile.reported", schemaVersion: 2 },
      availability: "AVAILABLE",
      release: {
        dataVersion: "8f401b48-5b0e-4a76-8d85-2c7101a28955",
        publishedAt: "2026-07-30T01:00:00.000Z",
        knowledgeCutoff: "2026-07-30T00:30:00.000Z",
        publicUsableAt: "2026-07-30T01:00:00.000Z",
        effectiveFrom: null,
        effectiveTo: null,
        methodology: { code: "reported", version: "2", kind: "REPORTED" },
        sources: [
          {
            sourceRef: "src_approved_etf_profile",
            publisher: "已批准 ETF 目录来源",
            sourceDataset: "ETF 产品资料",
            authoritative: true,
            redistribution: "INTERNAL_ONLY",
            coverageNote: null,
          },
        ],
        quality: { status: "PASSED", issueCodes: [] },
        completeness: "COMPLETE",
      },
      visibility: { mode: "CURRENT" },
      page: { limit: 50, hasMore: false, nextCursor: null },
      coverage: {},
      warnings: [],
      disclaimers: [],
    },
    records: [],
  };
}

/** 注入可用空页以及可选的 cursor 恢复提示状态。 */
function mockAvailableEmptyList(
  cursorRecoveryNotice = false,
  dismissCursorRecoveryNotice = vi.fn(),
): void {
  useEtfListMock.mockReturnValue({
    filters: {
      exchange: "SSE",
      q: "不存在的 ETF",
      sort: "symbol",
      order: "asc",
      page: 1,
      pageSize: 50,
    },
    query: {
      data: availableEmptyPage(),
      isFetching: false,
      isPending: false,
      isError: false,
      isPlaceholderData: false,
      refetch: vi.fn(),
    },
    applyFilters: vi.fn(),
    goToNextPage: vi.fn(),
    restartPagination: vi.fn(),
    resetFilters: vi.fn(),
    cursorRecoveryNotice,
    dismissCursorRecoveryNotice,
  });
}

describe("EtfListView", () => {
  /** AVAILABLE 筛选空态保留来源、发布时间和质量，不将其误报为无 publication。 */
  it("keeps publication metadata visible for an available empty filter result", () => {
    mockAvailableEmptyList();

    render(
      <MemoryRouter>
        <EtfListView />
      </MemoryRouter>,
    );

    expect(screen.getByText("没有匹配的 ETF")).toBeInTheDocument();
    expect(screen.getByText(/已批准 ETF 目录来源 · 发布于/u)).toBeInTheDocument();
    expect(screen.getByText("发布完整")).toBeInTheDocument();
    expect(screen.getByText("质量通过")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "ETF 产品目录" })).not.toBeInTheDocument();
  });

  /** 自动清除失效 cursor 后明确提示 publication 已更新且筛选条件仍被保留。 */
  it("shows the one-time cursor recovery notice", () => {
    const dismissCursorRecoveryNotice = vi.fn();
    mockAvailableEmptyList(true, dismissCursorRecoveryNotice);

    render(
      <MemoryRouter>
        <EtfListView />
      </MemoryRouter>,
    );

    expect(screen.getByText("目录已更新，已返回第一页并保留筛选条件")).toBeInTheDocument();
  });
});
