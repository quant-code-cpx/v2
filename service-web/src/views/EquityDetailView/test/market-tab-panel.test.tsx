import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { ApiError } from "../../../api/http";
import type { EquityBarPage } from "../../../types/equity-market";
import { MarketTabPanel } from "../components/MarketTabPanel";
import type { EquityDetailModel } from "../hooks/useEquityDetail";

const dataVersion = "8f401b48-5b0e-4a76-8d85-2c7101a28955";
const coverageVersion = "a6fb18c8-c0e1-4c66-9f57-cf3e12d83f12";
const sourceBatchId = "ef4d71e4-a122-43d3-96e2-706ec55ff1ca";

afterEach(cleanup);

/** 构造市场页签状态分支所需的最小详情模型，不加载图表引擎。 */
function createModel(input: { error?: unknown; bars?: unknown; barsStatus?: unknown }): {
  model: EquityDetailModel;
  retryBars: ReturnType<typeof vi.fn>;
} {
  const retryBars = vi.fn();
  const model = {
    exchange: "SSE",
    symbol: "600000",
    state: { period: "1d", adjust: "none", range: "1y" },
    updateState: vi.fn(),
    status: input.barsStatus === undefined ? undefined : { datasets: [input.barsStatus] },
    barsQuery: {
      isPending: false,
      isError: input.error !== undefined,
      error: input.error,
      isFetchNextPageError: false,
      isFetchingNextPage: false,
      hasNextPage: false,
      refetch: retryBars,
      fetchNextPage: vi.fn(),
    },
    bars: input.bars,
    statusQuery: { isPending: false, isError: false, isSuccess: false, refetch: vi.fn() },
    corporateActionsQuery: { isPending: false, isError: false, refetch: vi.fn() },
  } as unknown as EquityDetailModel;
  return { model, retryBars };
}

/** 构造符合公开严格合同的精确零记录 K 线覆盖。 */
function zeroRecordCoverage(): EquityBarPage {
  return {
    exchange: "SSE",
    symbol: "600000",
    coverageVersion,
    publicationKind: "ZERO_RECORD_COVERAGE",
    sourceBatchId,
    period: "1d",
    adjustmentMode: "none",
    adjustAsOf: null,
    factorVersion: null,
    formulaVersion: null,
    dataVersion,
    publishedAt: "2026-07-30T01:00:00.000Z",
    availability: "AVAILABLE",
    observedAt: null,
    reasonCode: null,
    qualityStatus: "passed",
    stale: false,
    items: [],
    nextCursor: null,
  };
}

/** 构造同一窗口曾成功读取、但不应在 publication 失效后回显的 DATA 行情缓存。 */
function cachedDataCoverage(): EquityBarPage {
  return {
    ...zeroRecordCoverage(),
    publicationKind: "DATA",
    items: [
      {
        periodEnd: "2026-07-30",
        open: "10.00",
        high: "10.20",
        low: "9.80",
        close: "10.10",
        volumeShares: "1000000",
        amountCny: "10100000.00",
        turnoverRate: "0.10",
        isFinal: true,
        revision: 1,
      },
    ],
  };
}

describe("MarketTabPanel", () => {
  /** 精确窗口没有 coverage 时独立说明，不显示通用失败或错误替代数据。 */
  it("renders a dedicated coverage-unavailable panel with a local retry", () => {
    const { model, retryBars } = createModel({
      error: new ApiError(409, "coverage-unavailable"),
      bars: zeroRecordCoverage(),
    });

    render(<MarketTabPanel model={model} />);

    expect(screen.getByText("当前窗口尚无精确 K 线覆盖")).toBeVisible();
    expect(screen.getByText(/不会以邻近日期、其他物理周期或 last-good 数据替代/u)).toBeVisible();
    expect(screen.queryByText(/K 线行情读取失败/u)).not.toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "K 线数据说明" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试行情" }));
    expect(retryBars).toHaveBeenCalledOnce();
  });

  /** 无 publication 保持既有独立状态，不能被精确窗口 coverage 失败覆盖。 */
  it("keeps NO_PUBLICATION separate from coverage-unavailable", () => {
    const { model } = createModel({
      error: new ApiError(503, "publication-unavailable"),
      barsStatus: {
        family: "BARS_1D",
        availability: "UNAVAILABLE",
        reasonCode: "NO_PUBLICATION",
        sourceLabel: null,
      },
    });

    render(<MarketTabPanel model={model} />);

    expect(screen.getByText(/NO_PUBLICATION/u)).toBeVisible();
    expect(screen.queryByText("当前窗口尚无精确 K 线覆盖")).not.toBeInTheDocument();
  });

  /** 无 publication 时不许把同 Query key 保留的 last-good DATA 交给 KLineChart。 */
  it("hides cached DATA bars when the requested publication becomes unavailable", () => {
    const { model } = createModel({
      error: new ApiError(503, "publication-unavailable"),
      bars: cachedDataCoverage(),
      barsStatus: {
        family: "BARS_1D",
        availability: "UNAVAILABLE",
        reasonCode: "NO_PUBLICATION",
        sourceLabel: null,
      },
    });

    render(<MarketTabPanel model={model} />);

    expect(screen.getByText(/NO_PUBLICATION/u)).toBeVisible();
    expect(screen.queryByRole("note", { name: "K 线数据说明" })).not.toBeInTheDocument();
    expect(screen.queryByText(`dataVersion ${dataVersion}`)).not.toBeInTheDocument();
  });

  /** 固定快照失效时，等待 data-status 刷新期间也不得继续呈现跨版本缓存。 */
  it("hides cached DATA bars when the requested snapshot expires", () => {
    const { model } = createModel({
      error: new ApiError(409, "snapshot-expired"),
      bars: cachedDataCoverage(),
    });

    render(<MarketTabPanel model={model} />);

    expect(screen.getByText(/K 线行情读取失败/u)).toBeVisible();
    expect(screen.queryByRole("note", { name: "K 线数据说明" })).not.toBeInTheDocument();
    expect(screen.queryByText(`dataVersion ${dataVersion}`)).not.toBeInTheDocument();
  });

  /** 零记录 publication 仍显示三项可复验谱系，且不把其解释为没有 publication。 */
  it("shows exact coverage lineage for a zero-record publication", () => {
    const { model } = createModel({ bars: zeroRecordCoverage() });

    render(<MarketTabPanel model={model} />);

    const explanation = screen.getByRole("note", { name: "K 线数据说明" });
    expect(explanation).toHaveTextContent("零记录精确覆盖");
    expect(explanation).toHaveTextContent(`coverageVersion ${coverageVersion}`);
    expect(explanation).toHaveTextContent("publicationKind ZERO_RECORD_COVERAGE");
    expect(explanation).toHaveTextContent(`sourceBatchId ${sourceBatchId}`);
    expect(screen.getByText(/已有精确零记录覆盖/u)).toBeVisible();
    expect(screen.queryByText(/数据集 BARS_1D 不可用/u)).not.toBeInTheDocument();
  });
});
