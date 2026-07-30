import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vite-plus/test";

import type { EtfProfileValues, MarketDataPage } from "../../../types/etf";
import { useEtfDetail } from "../hooks/useEtfDetail";

const apiMocks = vi.hoisted(() => ({
  queryEtfProfile: vi.fn(),
  queryEtfDailyBars: vi.fn(),
  queryEtfUnitNavs: vi.fn(),
  queryEtfTradingStates: vi.fn(),
}));

vi.mock("../../../api/etfs", () => ({
  /** 模拟产品资料后台刷新，保留真实 Query 状态机而不访问网络。 */
  queryEtfProfile: apiMocks.queryEtfProfile,
  /** 下游数据集在本测试中保持等待，避免影响产品资料断言。 */
  queryEtfDailyBars: apiMocks.queryEtfDailyBars,
  /** 下游数据集在本测试中保持等待，避免影响产品资料断言。 */
  queryEtfUnitNavs: apiMocks.queryEtfUnitNavs,
  /** 下游数据集在本测试中保持等待，避免影响产品资料断言。 */
  queryEtfTradingStates: apiMocks.queryEtfTradingStates,
}));

/** 构造 Hook 缓存使用的最小已校验 profile publication。 */
function cachedProfilePage(): MarketDataPage<EtfProfileValues> {
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
      coverage: {},
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

describe("useEtfDetail", () => {
  /** profile 后台刷新失败时 Query 保留缓存数据，Hook 继续输出永久 ETF 身份。 */
  it("retains the cached profile and identity after a background refresh error", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const page = cachedProfilePage();
    queryClient.setQueryData(["market-data", "etf", "profile", "SSE", "510300"], page, {
      updatedAt: 0,
    });
    apiMocks.queryEtfProfile.mockRejectedValue(new Error("profile refresh unavailable"));
    const pendingQuery = new Promise<never>(
      /** 保持非本测试目标的三个下游查询处于 pending。 */
      () => undefined,
    );
    apiMocks.queryEtfDailyBars.mockReturnValue(pendingQuery);
    apiMocks.queryEtfUnitNavs.mockReturnValue(pendingQuery);
    apiMocks.queryEtfTradingStates.mockReturnValue(pendingQuery);

    /** 提供真实路由参数与 QueryClient 缓存边界。 */
    function Wrapper({ children }: { children: ReactNode }) {
      return (
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={["/market/etfs/SSE/510300"]}>
            <Routes>
              <Route path="/market/etfs/:exchange/:symbol" element={<>{children}</>} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      );
    }

    const { result, unmount } = renderHook(useEtfDetail, { wrapper: Wrapper });

    await waitFor(() => expect(result.current.profileQuery.isError).toBe(true));
    expect(result.current.profileQuery.data).toBe(page);
    expect(result.current.profile).toEqual(page.records[0]?.values);

    unmount();
    queryClient.clear();
  });
});
