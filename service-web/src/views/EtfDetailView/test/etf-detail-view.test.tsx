import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vite-plus/test";

import type { EtfProfileValues, MarketDataPage } from "../../../types/etf";
import { EtfDetailView } from "../EtfDetailView";

const { useEtfDetailMock } = vi.hoisted(() => ({
  useEtfDetailMock: vi.fn(),
}));

vi.mock("../hooks/useEtfDetail", () => ({
  /** 让视图测试精确注入 TanStack Query 的缓存刷新失败状态。 */
  useEtfDetail: useEtfDetailMock,
}));

/** 构造一页已经通过浏览器严格合同校验的 ETF 产品资料。 */
function profilePage(): MarketDataPage<EtfProfileValues> {
  const dataVersion = "8f401b48-5b0e-4a76-8d85-2c7101a28955";
  const etfEntityRef = "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91";

  return {
    meta: {
      requestId: "72a4d2a1-3798-4bcf-978f-75c69c6d246b",
      contractVersion: "1.0.0",
      dataset: { code: "fund.etf.profile.reported", schemaVersion: 2 },
      availability: "AVAILABLE",
      release: {
        dataVersion,
        publishedAt: "2026-07-30T01:00:00.000Z",
        knowledgeCutoff: "2026-07-30T01:00:00.000Z",
        publicUsableAt: "2026-07-30T01:00:00.000Z",
        effectiveFrom: null,
        effectiveTo: null,
        methodology: { code: "reported", version: "2", kind: "REPORTED" },
        sources: [
          {
            sourceRef: "src_approved_etf_profile",
            publisher: "已批准 ETF 产品资料来源",
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
      page: { limit: 2, hasMore: false, nextCursor: null },
      coverage: { from: null, to: null, pitCoverage: "COMPLETE", gaps: [] },
      warnings: [],
      disclaimers: [],
    },
    records: [
      {
        recordRef: "fund.etf.profile.reported:SSE.510300",
        recordType: "ETF_PROFILE",
        entity: { entityRef: etfEntityRef, entityType: "ETF", identifiers: [] },
        time: { effectiveAt: "2026-07-30" },
        publicUsableAt: "2026-07-30T01:00:00.000Z",
        availabilityBasis: "SOURCE_PUBLICATION",
        sourcePublishedAt: null,
        observedAt: "2026-07-30T01:00:00.000Z",
        dataVersion,
        sourceRef: "src_approved_etf_profile",
        methodologyVersion: "2",
        qualityStatus: "PASSED",
        revision: { revisionNumber: 1, currentInPublication: true },
        values: {
          etfEntityRef,
          exchange: "SSE",
          symbol: "510300",
          displayName: "沪深300ETF",
          etfType: "ETF",
          managementMode: "INDEX_TRACKING",
          managerName: null,
          custodianName: null,
          listedOn: "2026-01-02",
          delistedOn: null,
          listingStatus: "LISTED",
          quoteCurrency: "CNY",
          navCurrency: "CNY",
          sourceTimePrecision: "DATE",
        },
      },
    ],
  };
}

describe("EtfDetailView", () => {
  /** 产品资料后台刷新失败时保留已校验缓存、显示独立警告并进入部分失败。 */
  it("keeps cached profile publication visible after a failed background refresh", async () => {
    const user = userEvent.setup();
    const refetchProfile = vi.fn();
    const page = profilePage();
    useEtfDetailMock.mockReturnValue({
      identity: { exchange: "SSE", symbol: "510300" },
      profile: page.records[0]?.values,
      profileQuery: {
        data: page,
        isPending: false,
        isError: true,
        refetch: refetchProfile,
      },
      barsQuery: { data: undefined, isPending: true, isError: false, refetch: vi.fn() },
      navsQuery: { data: undefined, isPending: true, isError: false, refetch: vi.fn() },
      statesQuery: { data: undefined, isPending: true, isError: false, refetch: vi.fn() },
    });

    render(
      <MemoryRouter>
        <EtfDetailView />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "沪深300ETF" })).toBeInTheDocument();
    expect(
      screen.getByText("产品资料刷新失败，仍展示上一份已校验 publication。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("部分数据集暂不可用；各区块独立重试，已成功 publication 继续保留。"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(refetchProfile).toHaveBeenCalledTimes(1);
  });
});
