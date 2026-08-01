import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { TargetSelectorEditor } from "../components/TargetSelectorEditor";

/** 验证目标选择器编辑器只暴露 capability 与严格合同允许的控件。 */
describe("TargetSelectorEditor", () => {
  /** 每个测试后卸载 MUI portal，避免下拉菜单污染下一次交互。 */
  afterEach(() => {
    cleanup();
  });

  /** 选择器类别下拉只使用目标数据集 capability 返回的允许值。 */
  it("renders only selector kinds declared by capability", async () => {
    const user = userEvent.setup();
    render(
      <TargetSelectorEditor
        idPrefix="selector-test"
        datasetCode="equity.daily"
        selector={{ kind: "GLOBAL" }}
        selectorKinds={["GLOBAL", "INSTRUMENT"]}
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByLabelText("业务范围"));

    expect(await screen.findByRole("option", { name: "全数据集" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "单证券" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "期货合约" })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
  });

  /** 从允许类别切换时创建合同规定的严格最小 union 分支。 */
  it("constructs a strict selector branch instead of arbitrary JSON", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-switch"
        datasetCode="equity.daily"
        selector={{ kind: "GLOBAL" }}
        selectorKinds={["GLOBAL", "INSTRUMENT"]}
        onChange={onChange}
      />,
    );

    await user.click(screen.getByLabelText("业务范围"));
    await user.click(await screen.findByRole("option", { name: "单证券" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "INSTRUMENT",
      exchange: "SSE",
      symbol: "",
    });
  });

  /** 两融资格快照只暴露深交所和北交所，并始终输出市场级 selector。 */
  it("renders BSE eligibility without an unsupported security subselector", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-margin-eligibility"
        datasetCode="market.margin.eligibility.reported"
        selector={{ kind: "MARGIN", operation: "ELIGIBILITY", venue: "BSE", security: null }}
        selectorKinds={["MARGIN"]}
        onChange={onChange}
      />,
    );

    expect(screen.getByLabelText("两融数据能力")).toHaveValue("ELIGIBILITY");
    expect(screen.getByLabelText("两融数据能力")).toBeDisabled();
    expect(screen.queryByLabelText("标的交易所")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("标的证券代码（可选）")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("市场"));
    expect(await screen.findByRole("option", { name: "SZSE" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "BSE" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "SSE" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "SZSE" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "MARGIN",
      operation: "ELIGIBILITY",
      venue: "SZSE",
      security: null,
    });
  });

  /** 两融市场日汇总不应向北交所泄漏选项，避免前端构造 data-sync 不支持的运行。 */
  it("keeps BSE unavailable for margin market and security operations", async () => {
    const user = userEvent.setup();
    render(
      <TargetSelectorEditor
        idPrefix="selector-margin-market"
        datasetCode="market.margin.market.1d.reported"
        selector={{ kind: "MARGIN", operation: "MARKET", venue: "SSE", security: null }}
        selectorKinds={["MARGIN"]}
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByLabelText("市场"));
    expect(await screen.findByRole("option", { name: "SSE" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "SZSE" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "BSE" })).not.toBeInTheDocument();
  });

  /** 沪深港通编辑器固定完整数据包操作并提供 ALL 全通道，不暴露越界操作。 */
  it("renders the complete stock connect bundle with ALL as the full-range channel", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-stock-connect"
        datasetCode="market.stock_connect.overview.bundle"
        selector={{
          kind: "STOCK_CONNECT",
          operation: "MARKET",
          channel: "ALL",
          direction: null,
        }}
        selectorKinds={["STOCK_CONNECT"]}
        onChange={onChange}
      />,
    );

    expect(screen.getByLabelText("操作")).toHaveValue("完整互联互通数据包");
    expect(screen.queryByText("ACTIVE_SECURITY")).not.toBeInTheDocument();
    expect(screen.queryByText("HOLDING")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("通道"));
    expect(await screen.findByRole("option", { name: "全部通道" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "沪通道" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "深通道" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "沪通道" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "STOCK_CONNECT",
      operation: "MARKET",
      channel: "SH",
      direction: null,
    });
  });

  /** 港通市场统计 `research` 明示无正式 `publication`，并始终输出与官方 `bundle` 独立的固定 `operation`。 */
  it("renders isolated stock-connect market-stat research controls", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-stock-connect-research"
        datasetCode="market.stock_connect.market_stat.research"
        selector={{
          kind: "STOCK_CONNECT_RESEARCH",
          operation: "MARKET_STAT",
          channel: "ALL",
          direction: null,
        }}
        selectorKinds={["STOCK_CONNECT_RESEARCH"]}
        onChange={onChange}
      />,
    );

    expect(screen.getByLabelText("操作")).toHaveValue("港通市场统计（research）");
    expect(screen.getByText("仅供 research；不产生正式 publication")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("完整互联互通数据包")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("研究通道"));
    expect(await screen.findByRole("option", { name: "全部通道" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "沪通道" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "深通道" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "沪通道" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "STOCK_CONNECT_RESEARCH",
      operation: "MARKET_STAT",
      channel: "SH",
      direction: null,
    });
  });

  /** 从兼容的单只 ETF 切换全量时只构造未冻结草稿，publication 版本必须由预检返回。 */
  it("constructs an ALL_ETFS draft without guessing ETF identities", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-etf-all"
        datasetCode="fund.etf.bar.1d.reported"
        selector={{
          kind: "ETF",
          operation: "BARS",
          venue: "SSE",
          etf: "SSE.510300",
        }}
        selectorKinds={["ETF"]}
        onChange={onChange}
      />,
    );

    expect(screen.getByLabelText("ETF 数据能力")).toHaveValue("BARS");
    expect(screen.getByLabelText("ETF 数据能力")).toBeDisabled();
    await user.click(screen.getByLabelText("ETF 同步范围"));
    await user.click(await screen.findByRole("option", { name: "全部已发布 ETF" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "ETF",
      operation: "BARS",
      venue: null,
      scope: "ALL_ETFS",
      etf: null,
      profileDataVersions: null,
    });
  });

  /** 单只 ETF 默认不重复填写 venue，并明确由 qualified identity 给出真实场所。 */
  it("explains that a single ETF venue comes from its qualified identity", async () => {
    const user = userEvent.setup();
    render(
      <TargetSelectorEditor
        idPrefix="selector-etf-one"
        datasetCode="fund.etf.bar.1d.reported"
        selector={{
          kind: "ETF",
          operation: "BARS",
          venue: null,
          etf: "SSE.510300",
        }}
        selectorKinds={["ETF"]}
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByLabelText("场所校验"));
    expect(
      await screen.findByRole("option", { name: "场所由显式 qualified identity 给出" }),
    ).toBeInTheDocument();
  });

  /** profile 编辑器提供显式沪深全市场范围，同时保留既有单市场选择器。 */
  it("constructs the explicit ALL_VENUES profile scope", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-etf-master"
        datasetCode="fund.etf.profile.reported"
        selector={{
          kind: "ETF",
          operation: "MASTER",
          venue: "SSE",
          etf: null,
        }}
        selectorKinds={["ETF"]}
        onChange={onChange}
      />,
    );

    await user.click(screen.getByLabelText("主数据同步范围"));
    await user.click(await screen.findByRole("option", { name: "沪深全市场" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "ETF",
      operation: "MASTER",
      venue: null,
      scope: "ALL_VENUES",
      etf: null,
    });
  });

  /** 指数目录的数据集绑定管理方与能力，且目录范围不渲染单指数代码输入。 */
  it("locks index catalog administrator and capability without an index code field", () => {
    render(
      <TargetSelectorEditor
        idPrefix="selector-index-catalog"
        datasetCode="index.csi.catalog.snapshot"
        selector={{
          kind: "INDEX",
          administrator: "CSI",
          capability: "index.catalog.snapshot",
          indexCode: null,
        }}
        selectorKinds={["INDEX"]}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("管理方")).toHaveValue("CSI");
    expect(screen.getByLabelText("管理方")).toBeDisabled();
    expect(screen.getByLabelText("能力")).toHaveValue("index.catalog.snapshot");
    expect(screen.getByLabelText("能力")).toBeDisabled();
    expect(screen.queryByLabelText("指数代码")).not.toBeInTheDocument();
  });

  /** 单指数快照只允许编辑指数代码，并在客户端规范为服务端要求的大写形式。 */
  it("edits only the index code for an index snapshot", () => {
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-index-snapshot"
        datasetCode="index.cni.weight.snapshot"
        selector={{
          kind: "INDEX",
          administrator: "CNI",
          capability: "index.weight.snapshot",
          indexCode: "",
        }}
        selectorKinds={["INDEX"]}
        onChange={onChange}
      />,
    );

    const indexCode = screen.getByRole("textbox", { name: /指数代码/ });
    expect(indexCode).toHaveAttribute("maxlength", "8");
    fireEvent.change(indexCode, { target: { value: "abc12345" } });

    expect(onChange).toHaveBeenCalledWith({
      kind: "INDEX",
      administrator: "CNI",
      capability: "index.weight.snapshot",
      indexCode: "ABC12345",
    });
  });

  /** 日频资金流切换范围时丢弃旧范围字段，并只构造服务端允许的个股严格 shape。 */
  it("constructs a strict daily money flow equity selector from the scope editor", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-money-flow-daily"
        datasetCode="money_flow.daily"
        selector={{
          kind: "MONEY_FLOW",
          operation: "DAILY",
          scope: "MARKET",
        }}
        selectorKinds={["MONEY_FLOW"]}
        onChange={onChange}
      />,
    );

    expect(screen.getByLabelText("操作")).toHaveValue("日频资金流");
    expect(screen.getByLabelText("操作")).toBeDisabled();
    await user.click(screen.getByLabelText("资金流范围"));
    await user.click(await screen.findByRole("option", { name: "个股" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "MONEY_FLOW",
      operation: "DAILY",
      scope: "EQUITY",
      exchange: "SSE",
      symbol: "",
    });
  });

  /** 东财个股排行明确开放 DAY_3，切换方法学时重建同花顺允许的最小范围。 */
  it("keeps money flow ranking windows and methodologies within their strict contract", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TargetSelectorEditor
        idPrefix="selector-money-flow-ranking"
        datasetCode="money_flow.ranking"
        selector={{
          kind: "MONEY_FLOW",
          operation: "RANKING",
          methodology: "EASTMONEY_ORDER_SIZE",
          scope: "EQUITY",
          window: "TODAY",
        }}
        selectorKinds={["MONEY_FLOW"]}
        onChange={onChange}
      />,
    );

    await user.click(screen.getByLabelText("窗口"));
    expect(await screen.findByRole("option", { name: "DAY_3" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "DAY_20" })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByLabelText("方法学"));
    await user.click(await screen.findByRole("option", { name: "同花顺交易方向" }));

    expect(onChange).toHaveBeenCalledWith({
      kind: "MONEY_FLOW",
      operation: "RANKING",
      methodology: "THS_TRADE_DIRECTION",
      scope: "EQUITY",
      window: "INTRADAY",
    });
  });
});
