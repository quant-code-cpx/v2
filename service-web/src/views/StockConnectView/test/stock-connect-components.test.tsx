import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";

import { ApiError } from "../../../api/http";
import type {
  StockConnectActiveSecurityPage,
  StockConnectAvailability,
  StockConnectRanking,
  StockConnectReadinessResponse,
  StockConnectTrendPoint,
  VersionedStockConnectResponse,
} from "../../../types/stock-connect";
import { ActiveSecurityTable } from "../components/ActiveSecurityTable";
import { StockConnectErrorState } from "../components/StockConnectRemoteState";
import { StockConnectReadinessNotice } from "../components/StockConnectReadinessNotice";
import { StockConnectTrendChart } from "../components/StockConnectTrendChart";
import type { StockConnectRankingSlug } from "../utils/stock-connect-url";

/** 在模块加载前创建可观测的图表引擎替身，验证真实 Hook 的挂载与销毁顺序。 */
const {
  chartDisposeMock,
  chartInitMock,
  chartSetOptionMock,
  resizeDisconnectMock,
  resizeObserveMock,
} = vi.hoisted(
  /** 为每次 ECharts 初始化返回一组实现同一最小引擎合同的可观测方法。 */
  () => {
    const chartDisposeMock = vi.fn();
    const chartResizeMock = vi.fn();
    const chartSetOptionMock = vi.fn();
    const resizeDisconnectMock = vi.fn();
    const resizeObserveMock = vi.fn();
    const chartInitMock = vi.fn(
      /** 每次画布重新挂载都返回独立生命周期可观察的引擎投影。 */
      () => ({
        dispose: chartDisposeMock,
        resize: chartResizeMock,
        setOption: chartSetOptionMock,
      }),
    );

    return {
      chartDisposeMock,
      chartInitMock,
      chartSetOptionMock,
      resizeDisconnectMock,
      resizeObserveMock,
    };
  },
);

vi.mock(
  "../../../libs/echarts",
  /** 用确定性引擎替身保留 `useECharts` 的真实 effect 和清理逻辑。 */
  () => ({
    echarts: {
      init: chartInitMock,
    },
  }),
);

/** 在 JSDOM 中记录图表尺寸观察器的订阅和销毁。 */
class TestResizeObserver {
  /** 接受真实 Hook 传入的尺寸回调；测试只验证观察器生命周期。 */
  public constructor(callback: ResizeObserverCallback) {
    void callback;
  }

  /** 记录开始观察当前图表容器。 */
  public observe(target: Element): void {
    void target;
    resizeObserveMock();
  }

  /** 接受单元素取消观察合同；当前 Hook 通过 disconnect 统一销毁。 */
  public unobserve(target: Element): void {
    void target;
  }

  /** 记录图表卸载时释放全部尺寸观察。 */
  public disconnect(): void {
    resizeDisconnectMock();
  }
}

/** 描述测试中固定使用的制度未披露金额事实。 */
interface UnavailableMoneyFactFixture {
  availability: "NOT_DISCLOSED_BY_REGIME";
  value: null;
  lineageRef: null;
}

/** 描述测试中固定使用的已报告原币金额事实。 */
interface ReportedMoneyFactFixture {
  availability: "REPORTED";
  value: {
    amount: string;
    currency: "CNY" | "HKD";
    unit: "BASE";
  };
  lineageRef: string;
}

/** 描述测试中由同源买卖金额派生的有符号净额事实。 */
interface DerivedMoneyFactFixture {
  availability: "DERIVED";
  value: {
    amount: string;
    currency: "CNY" | "HKD";
    unit: "BASE";
  };
  lineageRef: string;
}

/** 返回制度未披露且不伪造零值的金额事实。 */
function unavailableMoneyFact(): UnavailableMoneyFactFixture {
  return {
    availability: "NOT_DISCLOSED_BY_REGIME",
    value: null,
    lineageRef: null,
  };
}

/** 返回带来源 lineage 的原币已报告金额事实。 */
function reportedMoneyFact(
  amount: string,
  currency: "CNY" | "HKD" = "CNY",
): ReportedMoneyFactFixture {
  return {
    availability: "REPORTED",
    value: { amount, currency, unit: "BASE" },
    lineageRef: "lineage:stock-connect-component-test",
  };
}

/** 返回带派生方法 lineage 的原币有符号净额事实。 */
function derivedMoneyFact(
  amount: string,
  currency: "CNY" | "HKD" = "CNY",
): DerivedMoneyFactFixture {
  return {
    availability: "DERIVED",
    value: { amount, currency, unit: "BASE" },
    lineageRef: "lineage:stock-connect-component-test:buy-minus-sell-v1",
  };
}

/** 描述趋势测试点需要变化的金额和币种。 */
interface TrendPointOptions {
  hasAmount?: boolean;
  netCurrency?: "CNY" | "HKD";
  turnoverCurrency?: "CNY" | "HKD";
}

/** 构造一条满足公开合同的单通道日终趋势点。 */
function trendPoint({
  hasAmount = true,
  netCurrency = "CNY",
  turnoverCurrency = "CNY",
}: TrendPointOptions = {}): StockConnectTrendPoint {
  return {
    channel: "SH_NORTHBOUND",
    tradeDate: "2026-07-30",
    dataVersion: "bundle-v1",
    stats: {
      buyAmount: hasAmount ? reportedMoneyFact("490.00", turnoverCurrency) : unavailableMoneyFact(),
      sellAmount: hasAmount
        ? reportedMoneyFact("510.00", turnoverCurrency)
        : unavailableMoneyFact(),
      turnoverAmount: hasAmount
        ? reportedMoneyFact("1000.00", turnoverCurrency)
        : unavailableMoneyFact(),
      netBuyAmount: hasAmount ? derivedMoneyFact("-20.00", netCurrency) : unavailableMoneyFact(),
      tradeCount: {
        availability: "REPORTED",
        value: 10,
        lineageRef: "lineage:stock-connect-count",
      },
      etfTurnoverAmount: unavailableMoneyFact(),
    },
    status: {
      tradingDay: true,
      sessionState: "CLOSED",
      buyOrderAccepted: true,
      sellOrderAccepted: true,
      quotaState: "SUFFICIENT",
      quotaBalance: unavailableMoneyFact(),
      observedAt: "2026-07-30T18:00:00+08:00",
      finality: "END_OF_DAY",
    },
  };
}

/** 构造来源活跃榜或不可用净额榜的已校验响应。 */
function activeSecurityResponse(
  ranking: StockConnectRanking,
  rankingAvailability: StockConnectAvailability,
  includeSourceItem: boolean,
): VersionedStockConnectResponse<StockConnectActiveSecurityPage> {
  return {
    dataVersion: "bundle-v1",
    etag: '"bundle-v1"',
    data: {
      resolvedTradeDate: "2026-07-30",
      dateResolution: "EXACT",
      channel: "SH_SOUTHBOUND",
      ranking,
      rankingAvailability,
      rankingScope: "SOURCE_ACTIVE_SECURITIES_ONLY",
      items: includeSourceItem
        ? [
            {
              rankingRank: 1,
              sourceRank: 1,
              identity: {
                identityAvailability: "SOURCE_UNRESOLVED",
                instrumentEntityRef: null,
                sourceSecurityCode: "00001",
                displayName: "来源证券",
                listingVenue: "HKEX",
              },
              buyAmount: unavailableMoneyFact(),
              sellAmount: unavailableMoneyFact(),
              turnoverAmount: reportedMoneyFact("1200.00", "HKD"),
              netBuyAmount: unavailableMoneyFact(),
            },
          ]
        : [],
      nextCursor: null,
      publication: {
        bundleReleaseId: "635f6863-7008-4bcf-a69f-3e58e302b72c",
        dataVersion: "bundle-v1",
        tradeDate: "2026-07-30",
        publishedAt: "2026-07-30T18:15:00+08:00",
        qualityStatus: "APPROVED",
        qualityIssues: [],
        sourceRefs: [
          {
            sourceCode: "HKEX_DATA_MARKETPLACE",
            productName: "Stock Connect Daily Statistics",
            sourcePublicationAvailability: "REPORTED",
            sourcePublicationAt: "2026-07-30T18:00:00+08:00",
            sourceObservedAt: "2026-07-30T18:03:00+08:00",
            sourceFileSha256: "a".repeat(64),
          },
        ],
      },
    },
  };
}

/** 构造候选日执行失败但上一共同 publication 仍可独立读取的 readiness。 */
function failedReadiness(): StockConnectReadinessResponse {
  return {
    schemaVersion: "quant-v2.stock-connect-readiness.v1",
    dataVersion: "d".repeat(64),
    mode: "LATEST",
    selectedChannels: ["SH_NORTHBOUND"],
    requestedExactDate: null,
    candidateTradeDate: "2026-07-30",
    readyTradeDate: "2026-07-29",
    observedAt: "2026-07-30T18:15:00+08:00",
    calendar: {
      dataVersion: "b".repeat(64),
      observedAt: "2026-07-30T08:00:00+08:00",
      sourceFileSha256: "c".repeat(64),
      sourcePublicationAt: null,
      publicationAvailability: "NOT_REPORTED",
    },
    channels: [
      {
        channel: "SH_NORTHBOUND",
        calendarState: "OPEN",
        state: "FAILED",
        reasonCode: "EXECUTION_FAILED",
        bundleDataVersion: null,
        evidenceObservedAt: "2026-07-30T18:15:00+08:00",
      },
    ],
  };
}

/** 渲染指定榜单响应及 URL 排序选择，复用完整桌面表格结构。 */
function renderActiveSecurityTable(
  response: VersionedStockConnectResponse<StockConnectActiveSecurityPage>,
  ranking: StockConnectRankingSlug,
) {
  return (
    <MemoryRouter>
      <ActiveSecurityTable
        query={{
          data: response,
          error: null,
          isPending: false,
          isError: false,
          isFetching: false,
        }}
        ranking={ranking}
        pageSize={20}
        hasCursor={false}
        onRankingChange={vi.fn()}
        onPageSizeChange={vi.fn()}
        onFirstPage={vi.fn()}
        onNextPage={vi.fn()}
        onRetry={vi.fn()}
      />
    </MemoryRouter>
  );
}

/** 每个组件测试安装 JSDOM 缺失的尺寸观察器。 */
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", TestResizeObserver);
});

/** 每个组件测试卸载 React 树并清理全局替身和调用记录。 */
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** 覆盖互联互通关键降级状态、图表生命周期和可访问信息。 */
describe("stock connect components", () => {
  /** 有效、空值和混币状态切换必须销毁旧实例并在恢复后建立新实例。 */
  it("rebuilds and disposes the trend chart while preserving a readable point summary", () => {
    const validPoints = [trendPoint()];
    const { rerender, unmount } = render(
      <StockConnectTrendChart channel="SH_NORTHBOUND" points={validPoints} />,
    );

    expect(chartInitMock).toHaveBeenCalledTimes(1);
    expect(resizeObserveMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("img", { name: "沪股通日终成交额与可用净额趋势" })).toBeVisible();
    const summary = screen.getByRole("table", { name: "沪股通趋势逐点数据" });
    expect(within(summary).getByText("2026-07-30")).toBeInTheDocument();
    expect(within(summary).getByText("CNY 1,000.00")).toBeInTheDocument();
    expect(within(summary).getByText("− CNY 20.00 · 净卖出")).toBeInTheDocument();
    expect(chartSetOptionMock.mock.calls.at(-1)?.[0]).toMatchObject({ animation: false });

    rerender(
      <StockConnectTrendChart
        channel="SH_NORTHBOUND"
        points={[trendPoint({ hasAmount: false })]}
      />,
    );
    expect(screen.getByText("该 publication 无可绘制金额")).toBeInTheDocument();
    expect(chartDisposeMock).toHaveBeenCalledTimes(1);
    expect(resizeDisconnectMock).toHaveBeenCalledTimes(1);

    rerender(
      <StockConnectTrendChart
        channel="SH_NORTHBOUND"
        points={[trendPoint({ netCurrency: "HKD", turnoverCurrency: "CNY" })]}
      />,
    );
    expect(screen.getByText(/同一通道趋势出现多个币种/u)).toBeInTheDocument();
    expect(chartInitMock).toHaveBeenCalledTimes(1);

    rerender(<StockConnectTrendChart channel="SH_NORTHBOUND" points={validPoints} />);
    expect(chartInitMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("table", { name: "沪股通趋势逐点数据" })).toBeInTheDocument();

    unmount();
    expect(chartDisposeMock).toHaveBeenCalledTimes(2);
    expect(resizeDisconnectMock).toHaveBeenCalledTimes(2);
  });

  /** 来源榜缺少净额和净额榜不可用两种禁用态都必须提供真实存在的可聚焦说明。 */
  it("keeps every disabled net-ranking tab bound to an accessible reason", () => {
    const sourceResponse = activeSecurityResponse("SOURCE_ACTIVE", "REPORTED", true);
    const { rerender } = render(renderActiveSecurityTable(sourceResponse, "active"));

    let notice = screen.getByRole("alert");
    expect(notice).toHaveAttribute("id", "stock-connect-ranking-notice");
    expect(notice).toHaveAttribute("tabindex", "0");
    expect(notice).toHaveTextContent("当前来源活跃证券记录未同时报告买入和卖出");
    expect(screen.getByRole("tab", { name: "榜内净买入" })).toHaveAttribute(
      "aria-describedby",
      "stock-connect-ranking-notice",
    );
    expect(screen.getByRole("tab", { name: "榜内净买入" })).toBeDisabled();

    const unavailableNetResponse = activeSecurityResponse(
      "NET_BUY",
      "NOT_DISCLOSED_BY_REGIME",
      false,
    );
    rerender(renderActiveSecurityTable(unavailableNetResponse, "net-buy"));

    notice = screen.getByRole("alert");
    expect(notice).toHaveAttribute("id", "stock-connect-ranking-notice");
    expect(notice).toHaveTextContent("活跃榜内净买入当前不可展示");
    expect(screen.getByRole("tab", { name: "榜内净卖出" })).toHaveAttribute(
      "aria-describedby",
      "stock-connect-ranking-notice",
    );
  });

  /** readiness 独立说明候选日失败与上一共同发布日，不把旧值伪装成候选日。 */
  it("shows persisted candidate readiness separately from the ready publication date", () => {
    const { rerender } = render(
      <StockConnectReadinessNotice
        readiness={failedReadiness()}
        isPending={false}
        error={null}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByText("候选交易日：2026-07-30")).toBeInTheDocument();
    expect(screen.getByText("共同已发布日：2026-07-29")).toBeInTheDocument();
    expect(screen.getByText(/沪股通 · 失败 · 同步执行失败/u)).toBeInTheDocument();

    rerender(
      <StockConnectReadinessNotice
        readiness={undefined}
        isPending={false}
        error={new ApiError(409, "READINESS_NOT_OBSERVED")}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText(/不会根据当前时间猜测休市或失败原因/u)).toBeInTheDocument();
  });

  /** PUBLICATION_NOT_READY 只有精确日期选择才能提供返回 latest 的有效恢复动作。 */
  it("shows the latest recovery only for an exact-date publication error", () => {
    const onLatest = vi.fn();
    const error = new ApiError(409, "PUBLICATION_NOT_READY");
    const { rerender } = render(
      <StockConnectErrorState
        error={error}
        onRetry={vi.fn()}
        onLatest={onLatest}
        dateSelection="latest"
      />,
    );

    expect(screen.queryByRole("button", { name: "返回 latest" })).not.toBeInTheDocument();

    rerender(
      <StockConnectErrorState
        error={error}
        onRetry={vi.fn()}
        onLatest={onLatest}
        dateSelection="2026-07-30"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "返回 latest" }));
    expect(onLatest).toHaveBeenCalledTimes(1);
  });
});
