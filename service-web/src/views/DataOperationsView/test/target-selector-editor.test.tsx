import { cleanup, render, screen } from "@testing-library/react";
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
});
