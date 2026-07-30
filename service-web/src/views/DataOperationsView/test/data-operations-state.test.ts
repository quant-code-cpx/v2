import { describe, expect, it } from "vite-plus/test";

import {
  actorDisplayLabel,
  freshnessStatusLabel,
  isRunTerminal,
} from "../utils/data-operations-presentation";
import type { DatasetAvailability } from "../../../types/data-operations";
import {
  readDataOperationsUrlState,
  writeDataOperationsUrlState,
} from "../utils/data-operations-url";

describe("data operations state", () => {
  /** `INTERRUPTED` 仍可能被服务端恢复，页面不能停止轮询。 */
  it("keeps interrupted runs non-terminal", () => {
    expect(isRunTerminal("INTERRUPTED")).toBe(false);
    expect(isRunTerminal("SUCCEEDED")).toBe(true);
  });

  /** SYSTEM 操作人使用系统显示投影，且模型数据新鲜度明确显示不适用。 */
  it("renders SYSTEM actor and NOT_APPLICABLE without internal references", () => {
    expect(
      actorDisplayLabel({
        actorType: "SYSTEM",
        systemKind: "RECOVERY",
        actorId: null,
        displayName: "不应作为用户名称展示",
        deleted: false,
      }),
    ).toBe("系统恢复");
    expect(freshnessStatusLabel("NOT_APPLICABLE")).toBe("新鲜度不适用");
  });

  /** MODEL_ONLY 是可用性合同值，且必须与不适用的新鲜度并存而非降级为 UNKNOWN。 */
  it("keeps MODEL_ONLY availability distinct from unknown", () => {
    const availability: DatasetAvailability = "MODEL_ONLY";

    expect(availability).toBe("MODEL_ONLY");
    expect(freshnessStatusLabel("NOT_APPLICABLE")).toBe("新鲜度不适用");
  });

  /** URL 只保留白名单筛选和资源标识，不接受内部字段或异常状态。 */
  it("round-trips safe catalog state and rejects unsupported URL values", () => {
    const state = readDataOperationsUrlState(
      new URLSearchParams(
        "tab=runs&q=%E6%97%A5%E7%BA%BF&provider=provider-a&cursor=cursor-001&runStatus=INTERRUPTED&actorRef=forbidden",
      ),
    );

    expect(state).toMatchObject({
      tab: "runs",
      catalog: {
        limit: 50,
        query: "日线",
        providers: ["provider-a"],
        cursor: "cursor-001",
        runStatuses: ["INTERRUPTED"],
      },
      runStatus: "INTERRUPTED",
    });
    expect(writeDataOperationsUrlState(state).toString()).not.toContain("actorRef");
    expect(
      readDataOperationsUrlState(new URLSearchParams("runStatus=NOT_A_CONTRACT_STATUS")).runStatus,
    ).toBeUndefined();
  });

  /** 各公开资源必须保留独立 cursor，任何列表都不能复用目录分页令牌。 */
  it("keeps opaque cursors isolated by public resource", () => {
    const state = readDataOperationsUrlState(
      new URLSearchParams(
        "datasetCursor=dataset-1&runCursor=run-2&healthCursor=health-3&scheduleCursor=schedule-4&operationCursor=operation-5",
      ),
    );

    expect(state.catalog.cursor).toBe("dataset-1");
    expect(state.runCursor).toBe("run-2");
    expect(state.healthCursor).toBe("health-3");
    expect(state.scheduleCursor).toBe("schedule-4");
    expect(state.operationCursor).toBe("operation-5");
    expect(writeDataOperationsUrlState(state).toString()).toContain("runCursor=run-2");
  });
});
