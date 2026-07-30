import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vite-plus/test";

import { stockConnectPublicationSchema } from "../../../types/stock-connect";
import { SourceMethodDrawer } from "../components/SourceMethodDrawer";

/** 构造只包含一条来源引用的严格 publication。 */
function publicationWithSource(source: Record<string, unknown>) {
  return stockConnectPublicationSchema.parse({
    bundleReleaseId: "635f6863-7008-4bcf-a69f-3e58e302b72c",
    dataVersion: "bundle-v1",
    tradeDate: "2026-07-30",
    publishedAt: "2026-07-30T18:15:00+08:00",
    qualityStatus: "APPROVED",
    qualityIssues: [],
    sourceRefs: [source],
  });
}

/** 提供无副作用的受控抽屉关闭回调。 */
function handleClose(): void {}

/** 验证来源抽屉不会把接收时间显示成来源 publication 时间。 */
describe("SourceMethodDrawer", () => {
  /** 每个测试后卸载 Drawer portal，避免内容污染后续断言。 */
  afterEach(() => {
    cleanup();
  });

  /** 来源报告 publication 时分别展示来源发布时间与接收时间。 */
  it("separates a reported publication time from its observation time", () => {
    const publication = publicationWithSource({
      sourceCode: "HKEX_DATA_MARKETPLACE",
      productName: "Stock Connect Daily Statistics",
      sourcePublicationAvailability: "REPORTED",
      sourcePublicationAt: "2026-07-30T18:00:00+08:00",
      sourceObservedAt: "2026-07-30T18:03:00+08:00",
      sourceFileSha256: "a".repeat(64),
    });

    render(<SourceMethodDrawer open publication={publication} onClose={handleClose} />);

    expect(screen.getByText(/来源发布：2026\/07\/30 18:00/u)).toBeInTheDocument();
    expect(screen.getByText(/接收于 2026\/07\/30 18:03/u)).toBeInTheDocument();
  });

  /** 来源没有提供 publication 时只声明缺失，并明确展示真实接收时间。 */
  it("labels an unavailable source publication without substituting observedAt", () => {
    const publication = publicationWithSource({
      sourceCode: "HKEX_CALENDAR",
      productName: "Stock Connect Calendar",
      sourcePublicationAvailability: "NOT_PROVIDED_BY_SOURCE",
      sourcePublicationAt: null,
      sourceObservedAt: "2026-07-30T18:03:00+08:00",
      sourceFileSha256: null,
    });

    render(<SourceMethodDrawer open publication={publication} onClose={handleClose} />);

    expect(
      screen.getByText(/来源未提供 publication；接收于 2026\/07\/30 18:03/u),
    ).toBeInTheDocument();
    expect(screen.queryByText(/来源发布：/u)).not.toBeInTheDocument();
  });

  /** 判别合同拒绝“来源未提供”却同时携带发布时间的歧义记录。 */
  it("rejects a publication timestamp in the not-provided branch", () => {
    /** 构造违反判别约束的来源记录并触发 Zod 校验。 */
    const parseInvalidPublication = () =>
      publicationWithSource({
        sourceCode: "HKEX_CALENDAR",
        productName: "Stock Connect Calendar",
        sourcePublicationAvailability: "NOT_PROVIDED_BY_SOURCE",
        sourcePublicationAt: "2026-07-30T18:00:00+08:00",
        sourceObservedAt: "2026-07-30T18:03:00+08:00",
        sourceFileSha256: null,
      });

    expect(parseInvalidPublication).toThrow();
  });
});
