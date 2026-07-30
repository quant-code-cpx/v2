import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vite-plus/test";

import { DataOperationsRunBar } from "../components/DataOperationsRunBar";
import type { OperationsOverview } from "../../../types/data-operations";

/** 构造空闲执行槽的公开总览，覆盖无失败投递时的紧凑状态。 */
function idleOverview(): OperationsOverview {
  return {
    dataSync: {
      datasetCount: 0,
      enabledDatasetCount: 0,
      healthSummary: {
        status: "UNKNOWN",
        score: null,
        evaluatedAt: null,
        evaluationId: null,
        warningCount: 0,
        criticalCount: 0,
        openIssueCount: 0,
        affectedRecordCount: null,
      },
      executionSlot: {
        state: "IDLE",
        runId: null,
        datasetCode: null,
        leaseUntil: null,
        heartbeatAt: null,
      },
      queuedRunCount: 0,
      failedRunCount24h: 0,
      generatedAt: "2026-07-29T00:00:00.000Z",
    },
    deliveryPendingCount: 0,
    deliveryDeadLetterCount: 0,
  };
}

/** 归纳执行总览在空闲与异常投递状态下的紧凑展示规则。 */
describe("DataOperationsRunBar", () => {
  /** 无待处理失败时不显示零值占位或实现细节说明。 */
  it("keeps the idle summary compact", () => {
    render(
      <DataOperationsRunBar
        overview={idleOverview()}
        isLoading={false}
        isError={false}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.queryByText("待处理失败 0")).not.toBeInTheDocument();
    expect(screen.queryByText(/最近服务端快照/)).not.toBeInTheDocument();
  });
});
