import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { SectorsTabPanel } from "../components/SectorsTabPanel";
import type { EquityDetailModel } from "../hooks/useEquityDetail";

const statusVersion = "11111111-1111-4111-8111-111111111111";
const discoveryVersion = "22222222-2222-4222-8222-222222222222";

afterEach(cleanup);

/** 构造只覆盖申万组件版本门禁所需的最小详情模型。 */
function createModel(componentVersion: string) {
  const statusRefetch = vi.fn();
  const discoveryRefetch = vi.fn();
  const model = {
    status: {
      datasets: [
        {
          family: "SW_INDUSTRY_MEMBERSHIP",
          availability: "AVAILABLE",
          dataVersion: statusVersion,
        },
      ],
    },
    discovery: {
      availability: "AVAILABLE",
      components: [
        {
          family: "sw",
          availability: "AVAILABLE",
          dataVersion: componentVersion,
        },
      ],
    },
    discoveryRecord: {
      memberships: [
        {
          scheme: "SW2021_L1",
          code: "801120",
          name: "食品饮料",
          level: 1,
          observedOn: "2026-07-30",
        },
      ],
    },
    statusQuery: {
      isPending: false,
      isError: false,
      isSuccess: false,
      refetch: statusRefetch,
    },
    discoveryQuery: { refetch: discoveryRefetch },
    industryQuery: { isPending: false, isError: false },
    conceptQuery: { isPending: false, isError: false },
  } as unknown as EquityDetailModel;
  return { model, statusRefetch, discoveryRefetch };
}

describe("SectorsTabPanel", () => {
  /** 状态与 discovery 版本不一致时隐藏归属并允许同时刷新两条 publication。 */
  it("blocks SW memberships from a different discovery component version", () => {
    const { model, statusRefetch, discoveryRefetch } = createModel(discoveryVersion);

    render(<SectorsTabPanel model={model} />);

    expect(
      screen.getByText("申万状态与 discovery 组件版本不一致，暂不展示可能串版的归属。"),
    ).toBeVisible();
    expect(screen.queryByText("申万 1 级 · 食品饮料")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(statusRefetch).toHaveBeenCalledTimes(1);
    expect(discoveryRefetch).toHaveBeenCalledTimes(1);
  });

  /** 两端引用同一 SW publication 时才展示申万路径。 */
  it("renders SW memberships only when component versions match", () => {
    const { model } = createModel(statusVersion);

    render(<SectorsTabPanel model={model} />);

    expect(screen.getByText("申万 1 级 · 食品饮料")).toBeVisible();
    expect(
      screen.queryByText("申万状态与 discovery 组件版本不一致，暂不展示可能串版的归属。"),
    ).not.toBeInTheDocument();
  });
});
