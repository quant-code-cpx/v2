import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vite-plus/test";

import { DataOperationAuditPanel } from "../components/DataOperationAuditPanel";

/** 验证空操作记录仍在表格内呈现明确状态，而非只保留表头。 */
describe("DataOperationAuditPanel", () => {
  /** 服务端返回合法空页时显示表内空态和后续操作指引。 */
  it("renders an in-table empty state", () => {
    render(
      <DataOperationAuditPanel
        data={{ items: [], nextCursor: null }}
        isLoading={false}
        isError={false}
        onPageChange={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("暂无操作记录")).toBeInTheDocument();
    expect(screen.getByText("提交、取消或重试任务后的记录会显示在这里。")).toBeInTheDocument();
  });
});
