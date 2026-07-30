import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vite-plus/test";

import { FundCenterView } from "../FundCenterView";

describe("FundCenterView", () => {
  /** 基金入口只开放 ETF，并明确其他基金类型尚无数据合同。 */
  it("keeps ETF as the only phase-one data entry", () => {
    render(
      <MemoryRouter>
        <FundCenterView />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "基金与 ETF 中心" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "进入 ETF 目录" })).toHaveAttribute(
      "href",
      "/market/etfs",
    );
    expect(screen.getByText("场外公募基金")).toBeInTheDocument();
    expect(screen.getByText("LOF")).toBeInTheDocument();
    expect(screen.getByText("REITs")).toBeInTheDocument();
    expect(screen.getByText("货币基金")).toBeInTheDocument();
    expect(screen.getAllByText("尚未接入")).toHaveLength(5);
  });
});
